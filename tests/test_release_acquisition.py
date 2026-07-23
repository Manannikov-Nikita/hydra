from __future__ import annotations

from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import unittest

from hydra_codex.install_layout import validate_bundle
from hydra_codex.release_management import (
    activate_version,
    default_install_roots,
)
from tests.test_release_management import _bundle


class ReleaseAcquisitionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        self.environ = {"HOME": str(self.home)}
        self.roots = default_install_roots(self.home)
        first = _bundle(self.root, "2.3.4")
        installer = first.root / "install.sh"
        installer.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        installer.chmod(0o700)
        activate_version(first, roots=self.roots, environ=self.environ)
        self.active = self.roots.current.resolve()

    def assert_installer_lock_is_idle(self) -> None:
        lock = self.home / ".hydra-installer-lock"
        self.assertTrue(lock.is_file())
        self.assertEqual(lock.read_bytes(), b"hydra-installer-lock/v2\n")
        self.assertEqual(lock.stat().st_mode & 0o777, 0o600)
        self.assertEqual(
            list(self.home.glob(".hydra-installer-capability.*")),
            [],
        )

    def acquisition_api(self):
        try:
            from hydra_codex.release_acquisition import (
                ReleaseAcquisitionError,
                acquire_release_candidate,
            )
        except ModuleNotFoundError:
            self.fail("release acquisition module is missing")
        return ReleaseAcquisitionError, acquire_release_candidate

    def staged_candidate(self, version: str = "2.3.5") -> Path:
        staging = self.roots.home / ".acquire.fixture"
        staging.mkdir(mode=0o700)
        candidate = _bundle(staging, version)
        destination = staging / f"hydra-codex-{version}"
        candidate.root.rename(destination)
        return destination

    def test_acquisition_is_capability_bounded_validated_and_always_cleaned(
        self,
    ) -> None:
        _error, acquire = self.acquisition_api()
        observed: dict[str, object] = {}
        candidate: Path | None = None

        def runner(command, **options):
            nonlocal candidate
            candidate = self.staged_candidate()
            observed.update(command=command, **options)
            lock = self.home / ".hydra-installer-lock"
            self.assertTrue(lock.is_file())
            self.assertEqual(lock.read_bytes(), b"hydra-installer-lock/v2\n")
            capability = options["env"][
                "HYDRA_INTERNAL_RELEASE_ACQUISITION"
            ]
            capability_file = self.home / (
                ".hydra-installer-capability." + capability
            )
            self.assertEqual(
                capability_file.read_bytes(),
                b"hydra-installer-capability/v1\n",
            )
            self.assertEqual(capability_file.stat().st_mode & 0o777, 0o600)
            return subprocess.CompletedProcess(command, 0, str(candidate) + "\n", "")

        with acquire(
            environ={
                **self.environ,
                "PATH": str(self.root / "attacker-controlled-bin"),
                "PYTHONPATH": str(self.root / "attacker-controlled-python"),
                "PRIVATE_TOKEN": "must-not-be-inherited",
                "HYDRA_INSTALLER_RELEASE_BASE_URL": (
                    "http://127.0.0.1:43210/releases"
                ),
            },
            executable=self.active / "bin" / "hydra-codex",
            runner=runner,
            timeout=17,
            max_output_bytes=512,
        ) as acquired:
            assert candidate is not None
            self.assertEqual(acquired.current_version, "2.3.4")
            self.assertEqual(acquired.layout, validate_bundle(candidate))
            self.assertTrue(candidate.exists())

        self.assertEqual(
            observed["command"],
            [str(self.active / "install.sh"), "--acquire"],
        )
        self.assertEqual(observed["timeout"], 17)
        self.assertTrue(observed["capture_output"])
        self.assertTrue(observed["text"])
        environment = observed["env"]
        self.assertIsInstance(environment, dict)
        self.assertEqual(
            set(environment),
            {
                "HOME",
                "PATH",
                "LANG",
                "LC_ALL",
                "HYDRA_INSTALLER_RELEASE_BASE_URL",
                "HYDRA_INTERNAL_RELEASE_ACQUISITION",
            },
        )
        self.assertEqual(environment["PATH"], "/usr/bin:/bin:/usr/sbin:/sbin")
        self.assertEqual(environment["LANG"], "C")
        self.assertEqual(environment["LC_ALL"], "C")
        self.assertEqual(
            environment["HYDRA_INSTALLER_RELEASE_BASE_URL"],
            "http://127.0.0.1:43210/releases",
        )
        capability = environment["HYDRA_INTERNAL_RELEASE_ACQUISITION"]
        self.assertIsNotNone(re.fullmatch(r"[0-9a-f]{64}", capability))
        assert candidate is not None
        self.assertFalse(candidate.parent.exists())
        self.assert_installer_lock_is_idle()

    def test_acquisition_failures_are_path_private_and_release_all_owned_state(
        self,
    ) -> None:
        error_type, acquire = self.acquisition_api()
        private = str(self.root / "private-do-not-leak")

        def failed(command, **_options):
            return subprocess.CompletedProcess(command, 1, private + "\n", private)

        with self.assertRaises(error_type) as failure:
            with acquire(
                environ=self.environ,
                executable=self.active / "bin" / "hydra-codex",
                runner=failed,
            ):
                self.fail("failed acquisition yielded a candidate")
        self.assertNotIn(private, str(failure.exception))
        self.assert_installer_lock_is_idle()

        def timed_out(command, **options):
            raise subprocess.TimeoutExpired(
                command,
                options["timeout"],
                output=private,
                stderr=private,
            )

        with self.assertRaises(error_type) as timeout:
            with acquire(
                environ=self.environ,
                executable=self.active / "bin" / "hydra-codex",
                runner=timed_out,
            ):
                self.fail("timed-out acquisition yielded a candidate")
        self.assertNotIn(private, str(timeout.exception))
        self.assert_installer_lock_is_idle()

    def test_acquisition_rejects_oversized_or_out_of_root_output_without_deleting_it(
        self,
    ) -> None:
        error_type, acquire = self.acquisition_api()
        outside = self.root / "outside"
        outside.mkdir()

        for output in ("x" * 513, str(outside) + "\n"):
            with self.subTest(output=output[:20]):
                def runner(command, **_options):
                    return subprocess.CompletedProcess(command, 0, output, "")

                with self.assertRaises(error_type):
                    with acquire(
                        environ=self.environ,
                        executable=self.active / "bin" / "hydra-codex",
                        runner=runner,
                        max_output_bytes=512,
                    ):
                        self.fail("unsafe acquisition output was accepted")
                self.assertTrue(outside.exists())
                self.assert_installer_lock_is_idle()

    def test_acquisition_recovers_only_a_bounded_number_of_owned_staging_roots(
        self,
    ) -> None:
        error_type, acquire = self.acquisition_api()
        for index in range(8):
            stale = self.roots.home / f".acquire.stale{index}"
            stale.mkdir(mode=0o700)

        def runner(command, **_options):
            self.assertFalse(any(
                path.name.startswith(".acquire.stale")
                for path in self.roots.home.iterdir()
            ))
            candidate = self.staged_candidate()
            return subprocess.CompletedProcess(command, 0, str(candidate) + "\n", "")

        with acquire(
            environ=self.environ,
            executable=self.active / "bin" / "hydra-codex",
            runner=runner,
        ):
            pass

        for index in range(9):
            stale = self.roots.home / f".acquire.excess{index}"
            stale.mkdir(mode=0o700)
        called = False

        def forbidden_runner(command, **_options):
            nonlocal called
            called = True
            return subprocess.CompletedProcess(command, 1, "", "")

        with self.assertRaises(error_type):
            with acquire(
                environ=self.environ,
                executable=self.active / "bin" / "hydra-codex",
                runner=forbidden_runner,
            ):
                self.fail("excess stale roots were accepted")
        self.assertFalse(called)

    def test_default_runner_bounds_output_and_terminates_the_process_group(
        self,
    ) -> None:
        from hydra_codex.release_acquisition import (
            ReleaseAcquisitionError,
            _run_bounded_process,
        )

        child_pid = self.root / "child.pid"
        terminated = self.root / "child.terminated"
        child = (
            "import os,signal,sys\n"
            "pid,done=sys.argv[1:]\n"
            "open(pid,'w').write(str(os.getpid()))\n"
            "def stop(_signum,_frame):\n"
            " open(done,'w').write('terminated')\n"
            " os._exit(0)\n"
            "signal.signal(signal.SIGTERM,stop)\n"
            "while True: os.write(1,b'x'*4096)\n"
        )
        parent = (
            "import subprocess,sys\n"
            "process=subprocess.Popen([sys.executable,'-c',sys.argv[1],"
            "sys.argv[2],sys.argv[3]])\n"
            "process.wait()\n"
        )

        with self.assertRaises(ReleaseAcquisitionError):
            _run_bounded_process(
                [sys.executable, "-c", parent, child, str(child_pid), str(terminated)],
                environment={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
                timeout=3,
                maximum=128,
            )

        deadline = time.monotonic() + 1
        while time.monotonic() < deadline and not terminated.exists():
            time.sleep(0.01)
        self.assertTrue(terminated.exists())

    def test_check_resolves_latest_without_lock_download_or_staging(self) -> None:
        from hydra_codex.release_acquisition import (
            ReleaseAcquisitionError,
            resolve_latest_release,
        )

        before = tuple(
            sorted(
                (
                    path.relative_to(self.home).as_posix(),
                    path.lstat().st_mode,
                    path.read_bytes() if path.is_file() else None,
                )
                for path in self.home.rglob("*")
            ),
        )
        observed: dict[str, object] = {}

        def runner(command, **options):
            observed.update(command=command, **options)
            self.assertFalse((self.home / ".hydra-installer-lock").exists())
            self.assertFalse(any(
                path.name.startswith((".acquire.", ".hydra-download."))
                for path in self.home.rglob("*")
            ))
            return subprocess.CompletedProcess(
                command,
                0,
                '{"current_version":"2.3.4","latest_version":"2.3.5"}\n',
                "",
            )

        resolved = resolve_latest_release(
            environ=self.environ,
            executable=self.active / "bin" / "hydra-codex",
            runner=runner,
        )

        self.assertEqual(
            (resolved.current_version, resolved.latest_version),
            ("2.3.4", "2.3.5"),
        )
        self.assertEqual(
            observed["command"],
            [str(self.active / "install.sh"), "--resolve"],
        )
        self.assertNotIn(
            "HYDRA_INTERNAL_RELEASE_ACQUISITION",
            observed["env"],
        )
        self.assertIsNotNone(re.fullmatch(
            r"[0-9a-f]{64}",
            observed["env"]["HYDRA_INTERNAL_RELEASE_RESOLUTION"],
        ))
        after = tuple(
            sorted(
                (
                    path.relative_to(self.home).as_posix(),
                    path.lstat().st_mode,
                    path.read_bytes() if path.is_file() else None,
                )
                for path in self.home.rglob("*")
            ),
        )
        self.assertEqual(after, before)

        def public_human_output(command, **_options):
            return subprocess.CompletedProcess(
                command,
                0,
                "Hydra update available: 2.3.4 -> 2.3.5\n",
                "",
            )

        with self.assertRaises(ReleaseAcquisitionError):
            resolve_latest_release(
                environ=self.environ,
                executable=self.active / "bin" / "hydra-codex",
                runner=public_human_output,
            )


if __name__ == "__main__":
    unittest.main()
