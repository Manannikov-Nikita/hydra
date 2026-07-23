"""Acquire one verified release candidate through the bundled installer."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import secrets
import selectors
import shutil
import signal
import stat
import subprocess
import time

from .install_layout import BundleLayout, frozen_bundle_root, validate_bundle
from .release_management import default_install_roots, inspect_active_installation


_CAPABILITY_PATTERN = re.compile(r"[0-9a-f]{64}")
_STAGING_PATTERN = re.compile(r"\.acquire\.[A-Za-z0-9]{1,32}")
_STALE_LIMIT = 8
_DEFAULT_TIMEOUT = 180
_DEFAULT_OUTPUT_LIMIT = 4096


class ReleaseAcquisitionError(RuntimeError):
    """Raised when a candidate cannot be acquired without exposing private state."""


@dataclass(frozen=True)
class AcquiredRelease:
    layout: BundleLayout
    current_version: str


@dataclass(frozen=True)
class ResolvedRelease:
    current_version: str
    latest_version: str


Runner = Callable[..., subprocess.CompletedProcess[str]]


def _private_directory(path: Path) -> bool:
    try:
        details = path.lstat()
    except OSError:
        return False
    return (
        stat.S_ISDIR(details.st_mode)
        and not stat.S_ISLNK(details.st_mode)
        and stat.S_IMODE(details.st_mode) == 0o700
        and (not hasattr(os, "getuid") or details.st_uid == os.getuid())
    )


def _remove_staging(path: Path, roots_home: Path) -> None:
    if (
        path.parent != roots_home
        or _STAGING_PATTERN.fullmatch(path.name) is None
        or not _private_directory(path)
    ):
        raise ReleaseAcquisitionError("release acquisition cleanup failed")
    shutil.rmtree(path)


def _recover_stale(roots_home: Path) -> None:
    try:
        stale = tuple(sorted(
            (
                path
                for path in roots_home.iterdir()
                if path.name.startswith(".acquire.")
            ),
            key=lambda path: path.name,
        ))
    except OSError as error:
        raise ReleaseAcquisitionError("release acquisition cleanup failed") from error
    if len(stale) > _STALE_LIMIT:
        raise ReleaseAcquisitionError("too many stale release acquisitions")
    for path in stale:
        _remove_staging(path, roots_home)


def _owned_installer(active_root: Path) -> Path:
    installer = active_root / "install.sh"
    try:
        details = installer.lstat()
    except OSError as error:
        raise ReleaseAcquisitionError("bundled release installer is unavailable") from error
    if (
        not stat.S_ISREG(details.st_mode)
        or stat.S_ISLNK(details.st_mode)
        or details.st_mode & 0o111 == 0
        or (hasattr(os, "getuid") and details.st_uid != os.getuid())
    ):
        raise ReleaseAcquisitionError("bundled release installer is unavailable")
    return installer


def _candidate_path(
    output: str,
    *,
    roots_home: Path,
    maximum: int,
) -> tuple[Path, Path]:
    encoded = output.encode("utf-8", errors="replace")
    if (
        len(encoded) > maximum
        or not output.endswith("\n")
        or output.count("\n") != 1
        or "\r" in output
        or "\0" in output
    ):
        raise ReleaseAcquisitionError("release acquisition returned invalid output")
    candidate = Path(output[:-1])
    if not candidate.is_absolute():
        raise ReleaseAcquisitionError("release acquisition returned invalid output")
    staging = candidate.parent
    if (
        staging.parent != roots_home
        or _STAGING_PATTERN.fullmatch(staging.name) is None
        or not _private_directory(staging)
    ):
        raise ReleaseAcquisitionError("release acquisition returned unsafe staging")
    try:
        candidate_details = candidate.lstat()
    except OSError as error:
        raise ReleaseAcquisitionError("release candidate is unavailable") from error
    if not stat.S_ISDIR(candidate_details.st_mode) or stat.S_ISLNK(
        candidate_details.st_mode,
    ):
        raise ReleaseAcquisitionError("release candidate is unavailable")
    return candidate, staging


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except OSError as error:
        raise ReleaseAcquisitionError("release acquisition termination failed") from error
    deadline = time.monotonic() + 0.5
    while time.monotonic() < deadline:
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.01)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError as error:
            raise ReleaseAcquisitionError(
                "release acquisition termination failed",
            ) from error
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired as error:
        raise ReleaseAcquisitionError("release acquisition termination failed") from error


def _run_bounded_process(
    command: Sequence[str],
    *,
    environment: Mapping[str, str],
    timeout: float,
    maximum: int,
) -> subprocess.CompletedProcess[str]:
    process: subprocess.Popen[bytes] | None = None
    selector = selectors.DefaultSelector()
    try:
        process = subprocess.Popen(
            list(command),
            env=dict(environment),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        stdout = bytearray()
        stderr = bytearray()
        assert process.stdout is not None
        assert process.stderr is not None
        streams = (
            (process.stdout, stdout),
            (process.stderr, stderr),
        )
        for stream, destination in streams:
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, destination)
        deadline = time.monotonic() + timeout
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _terminate_process_group(process)
                raise ReleaseAcquisitionError("release acquisition timed out")
            for key, _events in selector.select(min(remaining, 0.1)):
                try:
                    chunk = os.read(key.fileobj.fileno(), 65536)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                    continue
                destination = key.data
                destination.extend(chunk)
                if len(destination) > maximum:
                    _terminate_process_group(process)
                    raise ReleaseAcquisitionError(
                        "release acquisition output exceeded its limit",
                    )
        returncode = process.wait(timeout=max(0.01, deadline - time.monotonic()))
        try:
            rendered_stdout = stdout.decode("utf-8")
            rendered_stderr = stderr.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ReleaseAcquisitionError(
                "release acquisition returned invalid output",
            ) from error
        return subprocess.CompletedProcess(
            list(command),
            returncode,
            rendered_stdout,
            rendered_stderr,
        )
    except ReleaseAcquisitionError:
        raise
    except (OSError, subprocess.SubprocessError) as error:
        if process is not None:
            _terminate_process_group(process)
        raise ReleaseAcquisitionError("release acquisition failed") from error
    finally:
        selector.close()
        if process is not None and process.poll() is None:
            _terminate_process_group(process)
        if process is not None:
            for stream in (process.stdout, process.stderr):
                if stream is not None and not stream.closed:
                    stream.close()


def _run(
    runner: Runner | None,
    command: Sequence[str],
    *,
    environment: Mapping[str, str],
    timeout: float,
    maximum: int,
) -> subprocess.CompletedProcess[str]:
    if runner is None:
        return _run_bounded_process(
            command,
            environment=environment,
            timeout=timeout,
            maximum=maximum,
        )
    try:
        return runner(
            list(command),
            env=dict(environment),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ReleaseAcquisitionError("release acquisition failed") from error


def _acquisition_environment(
    environ: Mapping[str, str],
    *,
    home: Path,
    capability: str | None,
) -> dict[str, str]:
    environment = {
        "HOME": str(home),
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "LANG": "C",
        "LC_ALL": "C",
    }
    if capability is not None:
        environment["HYDRA_INTERNAL_RELEASE_ACQUISITION"] = capability
    release_base = environ.get("HYDRA_INSTALLER_RELEASE_BASE_URL")
    if release_base is not None:
        match = re.fullmatch(
            r"http://(?:127\.0\.0\.1|\[::1\]):([0-9]{1,5})/releases",
            release_base,
        )
        if match is None or not 1 <= int(match.group(1)) <= 65535:
            raise ReleaseAcquisitionError("release acquisition source is invalid")
        environment["HYDRA_INSTALLER_RELEASE_BASE_URL"] = release_base
    return environment


def resolve_latest_release(
    *,
    environ: Mapping[str, str],
    executable: Path | None = None,
    runner: Runner | None = None,
    timeout: float = _DEFAULT_TIMEOUT,
    max_output_bytes: int = _DEFAULT_OUTPUT_LIMIT,
) -> ResolvedRelease:
    """Resolve latest release identity without downloading or mutating state."""
    if timeout <= 0 or max_output_bytes <= 0:
        raise ValueError("release resolution bounds must be positive")
    roots = default_install_roots(
        Path(environ["HOME"]).expanduser()
        if isinstance(environ.get("HOME"), str) and environ["HOME"]
        else Path.home(),
    )
    active = inspect_active_installation(roots)
    if active is None:
        raise ReleaseAcquisitionError("Hydra is not installed")
    current_version, _current_target = active
    runtime_root = frozen_bundle_root(executable)
    try:
        active_root = roots.current.resolve(strict=True)
    except OSError as error:
        raise ReleaseAcquisitionError("active release is unavailable") from error
    if runtime_root is None or runtime_root.resolve() != active_root:
        raise ReleaseAcquisitionError("active runtime identity is invalid")
    installer = _owned_installer(active_root)
    capability = secrets.token_hex(32)
    if _CAPABILITY_PATTERN.fullmatch(capability) is None:
        raise ReleaseAcquisitionError("release resolution capability failed")
    environment = _acquisition_environment(
        environ,
        home=roots.home.parent,
        capability=None,
    )
    environment["HYDRA_INTERNAL_RELEASE_RESOLUTION"] = capability
    result = _run(
        runner,
        (str(installer), "--resolve"),
        environment=environment,
        timeout=timeout,
        maximum=max_output_bytes,
    )
    if result.returncode != 0:
        raise ReleaseAcquisitionError("release resolution failed")
    if (
        len(result.stdout.encode("utf-8", errors="replace")) > max_output_bytes
        or len(result.stderr.encode("utf-8", errors="replace")) > max_output_bytes
        or result.stderr
    ):
        raise ReleaseAcquisitionError("release resolution failed")
    if not result.stdout.endswith("\n") or result.stdout.count("\n") != 1:
        raise ReleaseAcquisitionError("release resolution returned invalid output")
    try:
        payload = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError) as error:
        raise ReleaseAcquisitionError(
            "release resolution returned invalid output",
        ) from error
    if (
        not isinstance(payload, dict)
        or set(payload) != {"current_version", "latest_version"}
        or payload.get("current_version") != current_version
        or not isinstance(payload.get("latest_version"), str)
        or re.fullmatch(
            r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)",
            payload["latest_version"],
        )
        is None
        or json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
        != result.stdout
    ):
        raise ReleaseAcquisitionError("release resolution returned invalid output")
    latest_version = payload["latest_version"]
    return ResolvedRelease(current_version, latest_version)


@contextmanager
def acquire_release_candidate(
    *,
    environ: Mapping[str, str],
    executable: Path | None = None,
    runner: Runner | None = None,
    timeout: float = _DEFAULT_TIMEOUT,
    max_output_bytes: int = _DEFAULT_OUTPUT_LIMIT,
) -> Iterator[AcquiredRelease]:
    """Hold installer ownership while yielding one privately staged candidate."""
    if timeout <= 0 or max_output_bytes <= 0:
        raise ValueError("release acquisition bounds must be positive")
    roots = default_install_roots(
        Path(environ["HOME"]).expanduser()
        if isinstance(environ.get("HOME"), str) and environ["HOME"]
        else Path.home(),
    )
    active = inspect_active_installation(roots)
    if active is None:
        raise ReleaseAcquisitionError("Hydra is not installed")
    current_version, current_target = active
    runtime_root = frozen_bundle_root(executable)
    try:
        active_root = roots.current.resolve(strict=True)
    except OSError as error:
        raise ReleaseAcquisitionError("active release is unavailable") from error
    if runtime_root is None or runtime_root.resolve() != active_root:
        raise ReleaseAcquisitionError("active runtime identity is invalid")
    installer = _owned_installer(active_root)

    lock = roots.home.parent / ".hydra-installer-lock"
    try:
        lock.mkdir(mode=0o700)
    except OSError as error:
        raise ReleaseAcquisitionError("another installation is in progress") from error

    staging: Path | None = None
    try:
        _recover_stale(roots.home)
        capability = secrets.token_hex(32)
        if _CAPABILITY_PATTERN.fullmatch(capability) is None:
            raise ReleaseAcquisitionError("release acquisition capability failed")
        environment = _acquisition_environment(
            environ,
            home=roots.home.parent,
            capability=capability,
        )
        result = _run(
            runner,
            (str(installer), "--acquire"),
            environment=environment,
            timeout=timeout,
            maximum=max_output_bytes,
        )
        if (
            result.returncode != 0
            or len(result.stdout.encode("utf-8", errors="replace")) > max_output_bytes
            or len(result.stderr.encode("utf-8", errors="replace")) > max_output_bytes
            or result.stderr
        ):
            raise ReleaseAcquisitionError("release acquisition failed")
        candidate, staging = _candidate_path(
            result.stdout,
            roots_home=roots.home,
            maximum=max_output_bytes,
        )
        layout = validate_bundle(
            candidate,
            expected_target=current_target,
        )
        yield AcquiredRelease(layout, current_version)
    except ReleaseAcquisitionError:
        raise
    except Exception as error:
        raise ReleaseAcquisitionError("release acquisition failed") from error
    finally:
        cleanup_error: Exception | None = None
        try:
            if staging is not None and staging.exists():
                _remove_staging(staging, roots.home)
            _recover_stale(roots.home)
        except Exception as error:
            cleanup_error = error
        try:
            lock.rmdir()
        except OSError as error:
            cleanup_error = cleanup_error or error
        if cleanup_error is not None:
            raise ReleaseAcquisitionError(
                "release acquisition cleanup failed",
            ) from cleanup_error
