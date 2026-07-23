"""Owned, idempotent reconciliation of Hydra's supported Codex plugin state."""

from __future__ import annotations

import base64
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import re
import selectors
import shlex
import shutil
import stat
import subprocess
import tempfile
import time
from typing import Iterator, Mapping, Protocol


MARKETPLACE_NAME = "hydra"
PLUGIN_NAME = "hydra-codex"
PLUGIN_SELECTOR = "hydra-codex@hydra"
RECEIPT_SCHEMA_VERSION = 1
_RECEIPT_MAX_BYTES = 16 * 1024
_JOURNAL_SCHEMA_VERSION = 1
_JOURNAL_MAX_BYTES = 64 * 1024
_COMMAND_MAX_BYTES = 1024 * 1024
_VERSION_MAX_BYTES = 128
_VERSION_PUNCTUATION = frozenset(".!+-_")
_SAFE_PATH_PARTS = (
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "/usr/bin",
    "/bin",
    "/usr/sbin",
    "/sbin",
)
_CLI_NAME_PUNCTUATION = frozenset("-_.")
_MARKETPLACE_EMPTY = "No plugin marketplaces in scope."


class IntegrationError(RuntimeError):
    """Raised when Codex integration cannot reach or restore exact state."""


class IntegrationOwnershipError(IntegrationError):
    """Raised when Hydra cannot prove it owns the state it would change."""


class IncompatibleCodexError(IntegrationError):
    """Raised when the installed Codex lacks the required plugin capabilities."""


@dataclass(frozen=True)
class MarketplaceRecord:
    name: str
    source: Path


@dataclass(frozen=True)
class PluginRecord:
    name: str
    marketplace: str
    installed: bool
    version: str | None


@dataclass(frozen=True)
class IntegrationReport:
    changed: bool
    marketplace: str
    selector: str
    runtime_version: str


class CodexClient(Protocol):
    def version(self) -> str: ...

    def list_marketplaces(self) -> tuple[MarketplaceRecord, ...]: ...

    def add_marketplace(self, root: Path) -> None: ...

    def remove_marketplace(self, name: str) -> None: ...

    def list_plugins(
        self,
        marketplace: str,
        *,
        include_available: bool,
    ) -> tuple[PluginRecord, ...]: ...

    def add_plugin(self, selector: str) -> None: ...

    def remove_plugin(self, selector: str) -> None: ...


@dataclass(frozen=True)
class _Receipt:
    marketplace: str
    source: Path
    selector: str
    runtime_version: str
    schema_version: int = RECEIPT_SCHEMA_VERSION

    def payload(self) -> dict[str, object]:
        return {
            "marketplace": self.marketplace,
            "runtime_version": self.runtime_version,
            "schema_version": self.schema_version,
            "selector": self.selector,
            "source": str(self.source),
        }


@dataclass(frozen=True)
class CodexState:
    marketplace: MarketplaceRecord | None
    plugin: PluginRecord | None


@dataclass(frozen=True)
class _TransactionJournal:
    operation: str
    prior: CodexState
    desired: CodexState
    prior_receipt: bytes | None
    desired_receipt: _Receipt | None


def _run_bounded(
    arguments: list[str],
    *,
    environ: Mapping[str, str],
    timeout: float,
    max_output_bytes: int,
) -> tuple[int, bytes, bytes]:
    """Run one POSIX command while bounding time and combined pipe output."""
    if timeout <= 0 or max_output_bytes <= 0:
        raise ValueError("invalid subprocess bounds")
    process = subprocess.Popen(
        arguments,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=dict(environ),
    )
    assert process.stdout is not None
    assert process.stderr is not None
    streams = {
        process.stdout: bytearray(),
        process.stderr: bytearray(),
    }
    selector = selectors.DefaultSelector()
    deadline = time.monotonic() + timeout
    try:
        for stream in streams:
            selector.register(stream, selectors.EVENT_READ)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(arguments, timeout)
            events = selector.select(remaining)
            if not events:
                raise subprocess.TimeoutExpired(arguments, timeout)
            for key, _events in events:
                stream = key.fileobj
                chunk = os.read(stream.fileno(), 64 * 1024)
                if not chunk:
                    selector.unregister(stream)
                    continue
                streams[stream].extend(chunk)
                if sum(len(value) for value in streams.values()) > max_output_bytes:
                    raise subprocess.SubprocessError("Codex output limit exceeded")
        remaining = max(0.0, deadline - time.monotonic())
        returncode = process.wait(timeout=remaining)
        return (
            returncode,
            bytes(streams[process.stdout]),
            bytes(streams[process.stderr]),
        )
    except Exception:
        if process.poll() is None:
            process.kill()
        try:
            process.wait(timeout=1.0)
        except subprocess.SubprocessError:
            pass
        raise
    finally:
        selector.close()
        process.stdout.close()
        process.stderr.close()


def _safe_cli_name(value: object) -> bool:
    return bool(
        isinstance(value, str)
        and value
        and value.isascii()
        and value[0].isalnum()
        and all(
            character.isalnum() or character in _CLI_NAME_PUNCTUATION
            for character in value
        )
    )


def _safe_absolute_cli_path(value: object) -> bool:
    return bool(
        isinstance(value, str)
        and value
        and value == value.strip()
        and Path(value).is_absolute()
        and all(ord(character) >= 32 and ord(character) != 127 for character in value)
    )


def _strict_output_lines(raw: str) -> list[str]:
    if not raw.endswith("\n") or "\r" in raw or "\0" in raw:
        raise IncompatibleCodexError("Codex integration is unavailable")
    return raw[:-1].split("\n")


def _padded_table_value(field: str, *, allow_empty: bool = False) -> str:
    value = field.rstrip(" ")
    if (
        field != value + (" " * (len(field) - len(value)))
        or len(field) - len(value) < 2
        or (not allow_empty and not value)
        or value.startswith(" ")
    ):
        raise IncompatibleCodexError("Codex integration is unavailable")
    return value


def _parse_marketplace_listing(raw: str) -> tuple[MarketplaceRecord, ...]:
    lines = _strict_output_lines(raw)
    if lines == [_MARKETPLACE_EMPTY]:
        return ()
    if len(lines) < 2:
        raise IncompatibleCodexError("Codex integration is unavailable")
    header = re.fullmatch(r"MARKETPLACE(?P<gap> {2,})ROOT", lines[0])
    if header is None:
        raise IncompatibleCodexError("Codex integration is unavailable")
    root_start = header.start() + lines[0].index("ROOT")
    records: list[MarketplaceRecord] = []
    seen: set[str] = set()
    for line in lines[1:]:
        if len(line) <= root_start:
            raise IncompatibleCodexError("Codex integration is unavailable")
        name = _padded_table_value(line[:root_start])
        source_value = line[root_start:]
        if (
            not _safe_cli_name(name)
            or name in seen
            or not _safe_absolute_cli_path(source_value)
        ):
            raise IncompatibleCodexError("Codex integration is unavailable")
        seen.add(name)
        records.append(
            MarketplaceRecord(
                name,
                Path(source_value).expanduser().resolve(),
            ),
        )
    return tuple(records)


def _parse_plugin_listing(
    raw: str,
    *,
    marketplace: str,
    include_available: bool,
) -> tuple[PluginRecord, ...]:
    if not _safe_cli_name(marketplace):
        raise IncompatibleCodexError("Codex integration is unavailable")
    lines = _strict_output_lines(raw)
    if lines == [f"No plugins found in marketplace `{marketplace}`."]:
        return ()
    if (
        len(lines) < 5
        or lines[0] != f"Marketplace `{marketplace}`"
        or lines[2] != ""
        or not _safe_absolute_cli_path(lines[1])
    ):
        raise IncompatibleCodexError("Codex integration is unavailable")
    header_line = lines[3].rstrip(" ")
    header = re.fullmatch(
        r"PLUGIN(?P<plugin_gap> {2,})"
        r"STATUS(?P<status_gap> {2,})"
        r"VERSION(?P<version_gap> {2,})PATH",
        header_line,
    )
    if header is None:
        raise IncompatibleCodexError("Codex integration is unavailable")
    status_start = header_line.index("STATUS")
    version_start = header_line.index("VERSION")
    path_start = header_line.index("PATH")
    records: list[PluginRecord] = []
    seen: set[str] = set()
    for line in lines[4:]:
        if len(line) <= path_start:
            raise IncompatibleCodexError("Codex integration is unavailable")
        selector = _padded_table_value(line[:status_start])
        status_value = _padded_table_value(line[status_start:version_start])
        version_value = _padded_table_value(
            line[version_start:path_start],
            allow_empty=True,
        )
        plugin_path_field = line[path_start:]
        plugin_path = plugin_path_field.rstrip(" ")
        if (
            selector.count("@") != 1
            or selector in seen
            or not plugin_path
            or plugin_path.startswith(" ")
            or not _safe_absolute_cli_path(plugin_path)
        ):
            raise IncompatibleCodexError("Codex integration is unavailable")
        name, listed_marketplace = selector.rsplit("@", 1)
        if (
            not _safe_cli_name(name)
            or listed_marketplace != marketplace
        ):
            raise IncompatibleCodexError("Codex integration is unavailable")
        if status_value == "not installed":
            installed = False
            version = None
            if version_value:
                raise IncompatibleCodexError("Codex integration is unavailable")
        elif status_value in {"installed, enabled", "installed, disabled"}:
            installed = True
            version = version_value
            if not _is_safe_version_token(version):
                raise IncompatibleCodexError("Codex integration is unavailable")
        else:
            raise IncompatibleCodexError("Codex integration is unavailable")
        seen.add(selector)
        if include_available or installed:
            records.append(
                PluginRecord(name, marketplace, installed, version),
            )
    return tuple(records)


def _expect_exact_output(raw: str, expected: str) -> None:
    if raw != expected:
        raise IncompatibleCodexError("Codex integration is unavailable")


class CodexCommandClient:
    """Bounded subprocess adapter for the supported ``codex plugin`` surface."""

    def __init__(
        self,
        *,
        executable: str = "codex",
        environ: Mapping[str, str] | None = None,
        timeout: float = 10.0,
        max_output_bytes: int = _COMMAND_MAX_BYTES,
    ) -> None:
        values = dict(os.environ if environ is None else environ)
        if "CODEX_HOME" in values:
            codex_home = values["CODEX_HOME"]
            if (
                not isinstance(codex_home, str)
                or not codex_home
                or "\0" in codex_home
            ):
                raise IncompatibleCodexError(
                    "Codex integration is unavailable",
                )
        resolved = shutil.which(executable, path=values.get("PATH", ""))
        if resolved is None:
            raise IncompatibleCodexError("Codex integration is unavailable")
        self._executable = str(Path(resolved).resolve())
        self._timeout = timeout
        self._max_output_bytes = max_output_bytes
        allowed = (
            "HOME",
            "CODEX_HOME",
            "USER",
            "TMPDIR",
            "XDG_CONFIG_HOME",
            "XDG_DATA_HOME",
            "LANG",
            "LC_ALL",
            "SSL_CERT_FILE",
        )
        self._environ = {
            key: value
            for key in allowed
            if isinstance((value := values.get(key)), str) and value
        }
        safe_path = dict.fromkeys(
            (str(Path(self._executable).parent), *_SAFE_PATH_PARTS),
        )
        self._environ["PATH"] = os.pathsep.join(safe_path)

    def _run(self, arguments: list[str]) -> str:
        try:
            returncode, stdout, _stderr = _run_bounded(
                [self._executable, *arguments],
                environ=self._environ,
                timeout=self._timeout,
                max_output_bytes=self._max_output_bytes,
            )
            rendered = stdout.decode("utf-8")
        except (OSError, UnicodeDecodeError, ValueError, subprocess.SubprocessError):
            raise IncompatibleCodexError(
                "Codex integration is unavailable",
            ) from None
        if returncode != 0:
            raise IncompatibleCodexError(
                "Codex integration is unavailable",
            )
        return rendered

    def version(self) -> str:
        value = self._run(["--version"]).strip()
        if (
            not value
            or len(value.encode("utf-8")) > 256
            or "\n" in value
            or "\r" in value
            or any(ord(character) < 32 and character != "\t" for character in value)
        ):
            raise IncompatibleCodexError("Codex integration is unavailable")
        return value

    def list_marketplaces(self) -> tuple[MarketplaceRecord, ...]:
        return _parse_marketplace_listing(
            self._run(["plugin", "marketplace", "list"]),
        )

    def add_marketplace(self, root: Path) -> None:
        source = Path(root).expanduser().resolve()
        if not _safe_absolute_cli_path(str(source)):
            raise IncompatibleCodexError("Codex integration is unavailable")
        raw = self._run([
            "plugin", "marketplace", "add", str(source),
        ])
        _expect_exact_output(
            raw,
            f"Added marketplace `{MARKETPLACE_NAME}` from {source}.\n"
            f"Installed marketplace root: {source}\n",
        )

    def remove_marketplace(self, name: str) -> None:
        if name != MARKETPLACE_NAME:
            raise IncompatibleCodexError("Codex integration is unavailable")
        raw = self._run(["plugin", "marketplace", "remove", name])
        _expect_exact_output(raw, f"Removed marketplace `{name}`.\n")

    def list_plugins(
        self,
        marketplace: str,
        *,
        include_available: bool,
    ) -> tuple[PluginRecord, ...]:
        return _parse_plugin_listing(
            self._run([
                "plugin", "list", "--marketplace", marketplace,
            ]),
            marketplace=marketplace,
            include_available=include_available,
        )

    def add_plugin(self, selector: str) -> None:
        if selector != PLUGIN_SELECTOR:
            raise IncompatibleCodexError("Codex integration is unavailable")
        raw = self._run(["plugin", "add", selector])
        lines = _strict_output_lines(raw)
        expected = (
            f"Added plugin `{PLUGIN_NAME}` from marketplace "
            f"`{MARKETPLACE_NAME}`."
        )
        if (
            len(lines) != 2
            or lines[0] != expected
            or not lines[1].startswith("Installed plugin root: ")
            or lines[1] == "Installed plugin root: "
            or lines[1] != lines[1].strip()
            or not _safe_absolute_cli_path(
                lines[1].removeprefix("Installed plugin root: "),
            )
        ):
            raise IncompatibleCodexError("Codex integration is unavailable")

    def remove_plugin(self, selector: str) -> None:
        if selector != PLUGIN_SELECTOR:
            raise IncompatibleCodexError("Codex integration is unavailable")
        raw = self._run(["plugin", "remove", selector])
        _expect_exact_output(
            raw,
            f"Removed plugin `{PLUGIN_NAME}` from marketplace "
            f"`{MARKETPLACE_NAME}`.\n",
        )


def _one_marketplace(
    marketplaces: tuple[MarketplaceRecord, ...],
) -> MarketplaceRecord | None:
    matches = tuple(
        record for record in marketplaces if record.name == MARKETPLACE_NAME
    )
    if len(matches) > 1:
        raise IntegrationOwnershipError("Codex integration ownership is ambiguous")
    if not matches:
        return None
    record = matches[0]
    return MarketplaceRecord(record.name, record.source.expanduser().resolve())


def _one_plugin(plugins: tuple[PluginRecord, ...]) -> PluginRecord | None:
    matches = tuple(
        record
        for record in plugins
        if record.name == PLUGIN_NAME and record.marketplace == MARKETPLACE_NAME
    )
    if len(matches) > 1:
        raise IntegrationOwnershipError("Codex integration ownership is ambiguous")
    if not matches:
        return None
    record = matches[0]
    if record.version is not None and not _is_safe_version_token(record.version):
        raise IncompatibleCodexError("Codex integration is unavailable")
    return record


def inspect_codex(client: CodexClient) -> CodexState:
    """Inspect every required capability and return Hydra's current state."""
    try:
        version = client.version()
        if not isinstance(version, str) or not version.strip():
            raise IncompatibleCodexError("Codex integration is unavailable")
        marketplaces = client.list_marketplaces()
        plugins = client.list_plugins(
            MARKETPLACE_NAME,
            include_available=True,
        )
    except Exception:
        raise IncompatibleCodexError("Codex integration is unavailable") from None
    if not isinstance(marketplaces, tuple) or not isinstance(plugins, tuple):
        raise IncompatibleCodexError("Codex integration is unavailable")
    return CodexState(_one_marketplace(marketplaces), _one_plugin(plugins))


def _receipt_for(source: Path, runtime_version: str) -> _Receipt:
    if not _is_safe_version_token(runtime_version):
        raise IntegrationError("Hydra runtime version is invalid")
    return _Receipt(
        MARKETPLACE_NAME,
        source,
        PLUGIN_SELECTOR,
        runtime_version,
    )


def _ownership_error() -> IntegrationOwnershipError:
    return IntegrationOwnershipError("Codex integration ownership is ambiguous")


def _is_safe_version_token(value: object) -> bool:
    return bool(
        isinstance(value, str)
        and value
        and value.isascii()
        and value[0].isalnum()
        and len(value.encode("ascii")) <= _VERSION_MAX_BYTES
        and all(
            character.isalnum() or character in _VERSION_PUNCTUATION
            for character in value
        )
    )


def _open_flags(*, writable: bool = False, create: bool = False) -> int:
    flags = os.O_RDWR if writable else os.O_RDONLY
    if create:
        flags |= os.O_CREAT
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    return flags


def _read_private_bytes(path: Path, *, limit: int) -> bytes | None:
    """Read one private regular file through exactly one verified descriptor."""
    try:
        descriptor = os.open(path, _open_flags())
    except FileNotFoundError:
        return None
    except OSError:
        raise _ownership_error() from None
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size < 0
            or metadata.st_size > limit
            or os.name == "posix"
            and (
                metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) & 0o077
            )
        ):
            raise _ownership_error()
        content = bytearray()
        while len(content) <= limit:
            chunk = os.read(descriptor, min(64 * 1024, limit + 1 - len(content)))
            if not chunk:
                break
            content.extend(chunk)
        if len(content) > limit:
            raise _ownership_error()
        return bytes(content)
    except OSError:
        raise _ownership_error() from None
    finally:
        os.close(descriptor)


def _parse_receipt_bytes(raw: bytes) -> _Receipt:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise _ownership_error() from None
    keys = {
        "marketplace",
        "runtime_version",
        "schema_version",
        "selector",
        "source",
    }
    if (
        not isinstance(payload, Mapping)
        or set(payload) != keys
        or payload.get("marketplace") != MARKETPLACE_NAME
        or payload.get("selector") != PLUGIN_SELECTOR
        or payload.get("schema_version") != RECEIPT_SCHEMA_VERSION
        or not _is_safe_version_token(payload.get("runtime_version"))
        or not isinstance(payload.get("source"), str)
        or not Path(payload["source"]).is_absolute()
    ):
        raise _ownership_error()
    return _Receipt(
        MARKETPLACE_NAME,
        Path(payload["source"]).resolve(),
        PLUGIN_SELECTOR,
        payload["runtime_version"],
    )


def _read_receipt(path: Path) -> tuple[_Receipt | None, bytes | None]:
    raw = _read_private_bytes(path, limit=_RECEIPT_MAX_BYTES)
    if raw is None:
        return None, None
    return _parse_receipt_bytes(raw), raw


def _prepare_private_parent(parent: Path) -> None:
    try:
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        metadata = parent.lstat()
        if not stat.S_ISDIR(metadata.st_mode):
            raise _ownership_error()
        if os.name == "posix":
            if metadata.st_uid != os.getuid():
                raise _ownership_error()
            os.chmod(parent, 0o700)
    except IntegrationOwnershipError:
        raise
    except OSError:
        raise _ownership_error() from None


def _fsync_directory(parent: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(parent, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_receipt_bytes(path: Path, content: bytes) -> None:
    parent = path.parent
    _prepare_private_parent(parent)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".codex-integration-",
        dir=parent,
    )
    temporary = Path(temporary_name)
    descriptor_open = True
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor_open = False
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        _fsync_directory(parent)
    finally:
        if descriptor_open:
            try:
                os.close(descriptor)
            except OSError:
                pass
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _receipt_bytes(receipt: _Receipt) -> bytes:
    return (
        json.dumps(
            receipt.payload(),
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _write_receipt(path: Path, receipt: _Receipt) -> None:
    _write_receipt_bytes(path, _receipt_bytes(receipt))


def _delete_private_file(path: Path, *, limit: int) -> None:
    existing = _read_private_bytes(path, limit=limit)
    if existing is None:
        return
    try:
        os.unlink(path)
        _fsync_directory(path.parent)
    except OSError:
        raise _ownership_error() from None


def _delete_receipt(path: Path) -> None:
    _delete_private_file(path, limit=_RECEIPT_MAX_BYTES)


def _journal_path(receipt_path: Path) -> Path:
    return receipt_path.with_name(receipt_path.name + ".journal")


def _lock_path(receipt_path: Path) -> Path:
    return receipt_path.with_name(receipt_path.name + ".lock")


@contextmanager
def _integration_lock(receipt_path: Path) -> Iterator[None]:
    _prepare_private_parent(receipt_path.parent)
    path = _lock_path(receipt_path)
    try:
        descriptor = os.open(
            path,
            _open_flags(writable=True, create=True),
            0o600,
        )
    except OSError:
        raise _ownership_error() from None
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or os.name == "posix" and metadata.st_uid != os.getuid()
        ):
            raise _ownership_error()
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    except OSError:
        raise _ownership_error() from None
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _state_payload(state: CodexState) -> dict[str, object]:
    marketplace: dict[str, object] | None = None
    if state.marketplace is not None:
        marketplace = {
            "name": state.marketplace.name,
            "source": str(state.marketplace.source),
        }
    plugin: dict[str, object] | None = None
    if state.plugin is not None:
        plugin = {
            "installed": state.plugin.installed,
            "marketplace": state.plugin.marketplace,
            "name": state.plugin.name,
            "version": state.plugin.version,
        }
    return {"marketplace": marketplace, "plugin": plugin}


def _state_from_payload(payload: object) -> CodexState:
    if not isinstance(payload, Mapping) or set(payload) != {"marketplace", "plugin"}:
        raise _ownership_error()
    marketplace_payload = payload["marketplace"]
    marketplace = None
    if marketplace_payload is not None:
        if (
            not isinstance(marketplace_payload, Mapping)
            or set(marketplace_payload) != {"name", "source"}
            or marketplace_payload.get("name") != MARKETPLACE_NAME
            or not isinstance(marketplace_payload.get("source"), str)
            or not Path(marketplace_payload["source"]).is_absolute()
        ):
            raise _ownership_error()
        marketplace = MarketplaceRecord(
            MARKETPLACE_NAME,
            Path(marketplace_payload["source"]).resolve(),
        )
    plugin_payload = payload["plugin"]
    plugin = None
    if plugin_payload is not None:
        version = (
            plugin_payload.get("version")
            if isinstance(plugin_payload, Mapping)
            else object()
        )
        if (
            not isinstance(plugin_payload, Mapping)
            or set(plugin_payload)
            != {"installed", "marketplace", "name", "version"}
            or plugin_payload.get("name") != PLUGIN_NAME
            or plugin_payload.get("marketplace") != MARKETPLACE_NAME
            or not isinstance(plugin_payload.get("installed"), bool)
            or not (version is None or _is_safe_version_token(version))
            or marketplace is None
        ):
            raise _ownership_error()
        plugin = PluginRecord(
            PLUGIN_NAME,
            MARKETPLACE_NAME,
            plugin_payload["installed"],
            version,
        )
    return CodexState(marketplace, plugin)


def _journal_payload(journal: _TransactionJournal) -> dict[str, object]:
    return {
        "desired_receipt": (
            None
            if journal.desired_receipt is None
            else journal.desired_receipt.payload()
        ),
        "desired_state": _state_payload(journal.desired),
        "operation": journal.operation,
        "prior_receipt_b64": (
            None
            if journal.prior_receipt is None
            else base64.b64encode(journal.prior_receipt).decode("ascii")
        ),
        "prior_state": _state_payload(journal.prior),
        "schema_version": _JOURNAL_SCHEMA_VERSION,
    }


def _write_journal(path: Path, journal: _TransactionJournal) -> None:
    content = (
        json.dumps(
            _journal_payload(journal),
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    if len(content) > _JOURNAL_MAX_BYTES:
        raise IntegrationError("Codex integration transaction is invalid")
    _write_receipt_bytes(path, content)


def _read_journal(path: Path) -> _TransactionJournal | None:
    raw = _read_private_bytes(path, limit=_JOURNAL_MAX_BYTES)
    if raw is None:
        return None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise _ownership_error() from None
    keys = {
        "desired_receipt",
        "desired_state",
        "operation",
        "prior_receipt_b64",
        "prior_state",
        "schema_version",
    }
    if (
        not isinstance(payload, Mapping)
        or set(payload) != keys
        or payload.get("schema_version") != _JOURNAL_SCHEMA_VERSION
        or payload.get("operation") not in {"configure", "remove"}
    ):
        raise _ownership_error()
    encoded = payload.get("prior_receipt_b64")
    if encoded is None:
        prior_receipt = None
    elif isinstance(encoded, str):
        try:
            prior_receipt = base64.b64decode(encoded, validate=True)
        except (ValueError, UnicodeEncodeError):
            raise _ownership_error() from None
        if len(prior_receipt) > _RECEIPT_MAX_BYTES:
            raise _ownership_error()
        _parse_receipt_bytes(prior_receipt)
    else:
        raise _ownership_error()
    desired_payload = payload.get("desired_receipt")
    desired_receipt = None
    if desired_payload is not None:
        try:
            encoded_receipt = (
                json.dumps(
                    desired_payload,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8")
        except (TypeError, ValueError):
            raise _ownership_error() from None
        desired_receipt = _parse_receipt_bytes(encoded_receipt)
    journal = _TransactionJournal(
        payload["operation"],
        _state_from_payload(payload["prior_state"]),
        _state_from_payload(payload["desired_state"]),
        prior_receipt,
        desired_receipt,
    )
    if journal.operation == "configure":
        if journal.desired_receipt is None:
            raise _ownership_error()
        expected_desired = CodexState(
            MarketplaceRecord(
                MARKETPLACE_NAME,
                journal.desired_receipt.source,
            ),
            PluginRecord(
                PLUGIN_NAME,
                MARKETPLACE_NAME,
                True,
                journal.desired_receipt.runtime_version,
            ),
        )
        if journal.desired != expected_desired:
            raise _ownership_error()
    elif (
        journal.desired_receipt is not None
        or journal.desired != CodexState(None, None)
    ):
        raise _ownership_error()
    if journal.prior_receipt is not None:
        prior_owned = _parse_receipt_bytes(journal.prior_receipt)
        if (
            journal.prior.marketplace is not None
            and journal.prior.marketplace.source != prior_owned.source
        ):
            raise _ownership_error()
    if (
        journal.prior.plugin is not None
        and journal.prior.marketplace is None
    ):
        raise _ownership_error()
    if (
        journal.prior_receipt is None
        and journal.prior != CodexState(None, None)
    ):
        raise _ownership_error()
    if journal.operation == "remove" and journal.prior_receipt is None:
        raise _ownership_error()
    return journal


def _state_is_exact(state: CodexState, receipt: _Receipt) -> bool:
    return bool(
        state.marketplace is not None
        and state.marketplace.source == receipt.source
        and state.plugin is not None
        and state.plugin.installed
        and state.plugin.version == receipt.runtime_version
    )


def _ensure_owned(
    state: CodexState,
    receipt: _Receipt | None,
) -> None:
    has_state = state.marketplace is not None or bool(
        state.plugin is not None and state.plugin.installed
    )
    if has_state and receipt is None:
        raise IntegrationOwnershipError(
            "Codex integration ownership is ambiguous",
        )
    if receipt is None:
        return
    if (
        state.marketplace is not None
        and state.marketplace.source != receipt.source
    ):
        raise IntegrationOwnershipError(
            "Codex integration ownership is ambiguous",
        )
    if (
        state.plugin is not None
        and state.plugin.installed
        and state.marketplace is None
    ):
        raise IntegrationOwnershipError(
            "Codex integration ownership is ambiguous",
        )


def _known_snapshots(journal: _TransactionJournal) -> set[CodexState]:
    snapshots = {journal.prior, journal.desired, CodexState(None, None)}
    for state in (journal.prior, journal.desired):
        if state.marketplace is None:
            continue
        snapshots.add(CodexState(state.marketplace, None))
        if state.plugin is not None:
            snapshots.add(
                CodexState(
                    state.marketplace,
                    PluginRecord(
                        PLUGIN_NAME,
                        MARKETPLACE_NAME,
                        False,
                        state.plugin.version,
                    ),
                ),
            )
    return snapshots


def _state_is_known(
    state: CodexState,
    journal: _TransactionJournal,
) -> bool:
    if state in _known_snapshots(journal):
        return True
    if (
        state.marketplace is None
        or state.plugin is None
    ):
        return False
    if state.plugin.installed:
        return bool(
            journal.operation == "configure"
            and state.marketplace == journal.desired.marketplace
            and journal.desired.plugin is not None
            and journal.desired.plugin.installed
            and state.plugin.name == PLUGIN_NAME
            and state.plugin.marketplace == MARKETPLACE_NAME
            and _is_safe_version_token(state.plugin.version)
        )
    return state.marketplace.source in {
        snapshot.marketplace.source
        for snapshot in (journal.prior, journal.desired)
        if snapshot.marketplace is not None
    }


def _inspect_known(
    client: CodexClient,
    journal: _TransactionJournal,
) -> CodexState:
    state = inspect_codex(client)
    if not _state_is_known(state, journal):
        raise _ownership_error()
    return state


def _guard_expected(
    client: CodexClient,
    expected: CodexState,
    journal: _TransactionJournal,
) -> None:
    observed = _inspect_known(client, journal)
    if observed != expected:
        raise _ownership_error()


def _restore_state(
    client: CodexClient,
    journal: _TransactionJournal,
) -> None:
    target = journal.prior
    current = _inspect_known(client, journal)
    if current == target:
        return
    if current.plugin is not None and current.plugin.installed:
        _guard_expected(client, current, journal)
        client.remove_plugin(PLUGIN_SELECTOR)
        current = _inspect_known(client, journal)
    if current.marketplace is not None and current != target:
        _guard_expected(client, current, journal)
        client.remove_marketplace(MARKETPLACE_NAME)
        current = _inspect_known(client, journal)
    if target.marketplace is not None and current.marketplace is None:
        _guard_expected(client, current, journal)
        client.add_marketplace(target.marketplace.source)
        current = _inspect_known(client, journal)
    if (
        target.plugin is not None
        and target.plugin.installed
        and not bool(current.plugin is not None and current.plugin.installed)
    ):
        if (
            current.plugin is None
            or (
                current.plugin.version is not None
                and current.plugin.version != target.plugin.version
            )
        ):
            raise IntegrationError("Codex integration live rollback failed")
        _guard_expected(client, current, journal)
        client.add_plugin(PLUGIN_SELECTOR)
        current = _inspect_known(client, journal)
    if current != target:
        raise IntegrationError("Codex integration live rollback failed")


def _validate_recovery_receipt(
    receipt_path: Path,
    journal: _TransactionJournal,
) -> bytes | None:
    current = _read_private_bytes(receipt_path, limit=_RECEIPT_MAX_BYTES)
    allowed: set[bytes | None] = {journal.prior_receipt}
    if journal.operation == "configure":
        assert journal.desired_receipt is not None
        allowed.add(_receipt_bytes(journal.desired_receipt))
    else:
        allowed.add(None)
    if current not in allowed:
        raise _ownership_error()
    return current


def _restore_original_receipt(
    receipt_path: Path,
    journal: _TransactionJournal,
) -> None:
    current = _validate_recovery_receipt(receipt_path, journal)
    if current == journal.prior_receipt:
        return
    if journal.prior_receipt is None:
        _delete_receipt(receipt_path)
        if _read_private_bytes(receipt_path, limit=_RECEIPT_MAX_BYTES) is not None:
            raise IntegrationError("Codex integration receipt restore failed")
        return
    _write_receipt_bytes(receipt_path, journal.prior_receipt)
    restored = _read_private_bytes(receipt_path, limit=_RECEIPT_MAX_BYTES)
    if restored != journal.prior_receipt:
        raise IntegrationError("Codex integration receipt restore failed")


def _recover_transaction(
    client: CodexClient,
    receipt_path: Path,
    journal: _TransactionJournal,
) -> None:
    _validate_recovery_receipt(receipt_path, journal)
    live_error: Exception | None = None
    receipt_error: Exception | None = None
    try:
        _restore_state(client, journal)
    except Exception as error:
        live_error = error
    try:
        _restore_original_receipt(receipt_path, journal)
    except Exception as error:
        receipt_error = error
    if live_error is None and receipt_error is None:
        try:
            _delete_private_file(
                _journal_path(receipt_path),
                limit=_JOURNAL_MAX_BYTES,
            )
            return
        except Exception:
            raise IntegrationError(
                "Codex integration recovery failed: journal finalization failed",
            ) from None
    outcomes = [
        "live rollback failed" if live_error is not None else "live rollback restored",
        (
            "receipt ownership ambiguous"
            if isinstance(receipt_error, IntegrationOwnershipError)
            else "receipt restore failed"
            if receipt_error is not None
            else "receipt restored"
        ),
    ]
    if live_error is None and isinstance(receipt_error, IntegrationOwnershipError):
        raise receipt_error
    raise IntegrationError(
        "Codex integration recovery failed: " + "; ".join(outcomes),
    ) from None


def _recover_pending_transaction(
    client: CodexClient,
    receipt_path: Path,
) -> None:
    journal = _read_journal(_journal_path(receipt_path))
    if journal is not None:
        _recover_transaction(client, receipt_path, journal)


def configure_codex(
    *,
    client: CodexClient,
    marketplace_root: Path,
    runtime_version: str,
    receipt_path: Path,
    refresh: bool,
) -> IntegrationReport:
    """Reconcile the owned Hydra marketplace and plugin to one exact version."""
    source = Path(marketplace_root).expanduser().resolve()
    desired = _receipt_for(source, runtime_version)
    receipt_target = Path(receipt_path).expanduser()
    with _integration_lock(receipt_target):
        _recover_pending_transaction(client, receipt_target)
        owned, original_receipt = _read_receipt(receipt_target)
        current = inspect_codex(client)
        _ensure_owned(current, owned)
        if _state_is_exact(current, desired) and owned == desired:
            return IntegrationReport(
                False,
                MARKETPLACE_NAME,
                PLUGIN_SELECTOR,
                runtime_version,
            )
        if (
            owned is not None
            and (
                owned.runtime_version != runtime_version
                or owned.source != source
            )
            and not refresh
        ):
            raise IntegrationError("Codex integration refresh is required")

        desired_state = CodexState(
            MarketplaceRecord(MARKETPLACE_NAME, source),
            PluginRecord(
                PLUGIN_NAME,
                MARKETPLACE_NAME,
                True,
                runtime_version,
            ),
        )
        if current == desired_state:
            _write_receipt(receipt_target, desired)
            verified_receipt, _raw = _read_receipt(receipt_target)
            if verified_receipt != desired:
                raise IntegrationError("Codex integration verification failed")
            return IntegrationReport(
                True,
                MARKETPLACE_NAME,
                PLUGIN_SELECTOR,
                runtime_version,
            )

        journal = _TransactionJournal(
            "configure",
            current,
            desired_state,
            original_receipt,
            desired,
        )
        _write_journal(_journal_path(receipt_target), journal)
        mutation_started = False
        try:
            plugin_version_mismatch = bool(
                current.plugin is not None
                and current.plugin.version != runtime_version
            )
            replace_marketplace = bool(
                current.marketplace is not None
                and (
                    current.marketplace.source != source
                    or plugin_version_mismatch
                )
            )
            replace_plugin = bool(
                current.plugin is not None
                and current.plugin.installed
                and (
                    current.plugin.version != runtime_version
                    or replace_marketplace
                )
            )
            if replace_plugin:
                _guard_expected(client, current, journal)
                mutation_started = True
                client.remove_plugin(PLUGIN_SELECTOR)
                current = _inspect_known(client, journal)
            if replace_marketplace:
                _guard_expected(client, current, journal)
                mutation_started = True
                client.remove_marketplace(MARKETPLACE_NAME)
                current = _inspect_known(client, journal)
            if current.marketplace is None:
                _guard_expected(client, current, journal)
                mutation_started = True
                client.add_marketplace(source)
                current = _inspect_known(client, journal)
            if (
                current.plugin is None
                or (
                    current.plugin.version is not None
                    and current.plugin.version != runtime_version
                )
            ):
                raise IntegrationError(
                    "Bundled plugin version does not match Hydra runtime",
                )
            if not current.plugin.installed:
                _guard_expected(client, current, journal)
                mutation_started = True
                client.add_plugin(PLUGIN_SELECTOR)
                current = _inspect_known(client, journal)
            verified = inspect_codex(client)
            if verified != desired_state or not _state_is_exact(verified, desired):
                raise IntegrationError("Codex integration verification failed")
            _write_receipt(receipt_target, desired)
            verified_receipt, _raw = _read_receipt(receipt_target)
            if verified_receipt != desired:
                raise IntegrationError("Codex integration verification failed")
            _delete_private_file(
                _journal_path(receipt_target),
                limit=_JOURNAL_MAX_BYTES,
            )
        except Exception as error:
            if isinstance(error, IntegrationOwnershipError) and not mutation_started:
                _delete_private_file(
                    _journal_path(receipt_target),
                    limit=_JOURNAL_MAX_BYTES,
                )
                raise
            _recover_transaction(client, receipt_target, journal)
            if isinstance(error, IntegrationError):
                raise error
            raise IntegrationError("Codex integration update failed") from None
        return IntegrationReport(
            True,
            MARKETPLACE_NAME,
            PLUGIN_SELECTOR,
            runtime_version,
        )


def remove_codex_integration(
    *,
    client: CodexClient,
    receipt_path: Path,
) -> IntegrationReport:
    """Detach only receipt-owned Hydra state and preserve every unrelated file."""
    target = Path(receipt_path).expanduser()
    with _integration_lock(target):
        _recover_pending_transaction(client, target)
        owned, original_receipt = _read_receipt(target)
        current = inspect_codex(client)
        _ensure_owned(current, owned)
        if owned is None:
            return IntegrationReport(False, MARKETPLACE_NAME, PLUGIN_SELECTOR, "")
        if current == CodexState(None, None):
            _delete_receipt(target)
            return IntegrationReport(
                True,
                MARKETPLACE_NAME,
                PLUGIN_SELECTOR,
                owned.runtime_version,
            )
        journal = _TransactionJournal(
            "remove",
            current,
            CodexState(None, None),
            original_receipt,
            None,
        )
        _write_journal(_journal_path(target), journal)
        mutation_started = False
        try:
            if current.plugin is not None and current.plugin.installed:
                _guard_expected(client, current, journal)
                mutation_started = True
                client.remove_plugin(PLUGIN_SELECTOR)
                current = _inspect_known(client, journal)
            if current.marketplace is not None:
                _guard_expected(client, current, journal)
                mutation_started = True
                client.remove_marketplace(MARKETPLACE_NAME)
                current = _inspect_known(client, journal)
            if current != CodexState(None, None):
                raise IntegrationError(
                    "Codex integration removal verification failed",
                )
            _delete_receipt(target)
            if _read_private_bytes(target, limit=_RECEIPT_MAX_BYTES) is not None:
                raise IntegrationError(
                    "Codex integration removal verification failed",
                )
            _delete_private_file(
                _journal_path(target),
                limit=_JOURNAL_MAX_BYTES,
            )
        except Exception as error:
            if isinstance(error, IntegrationOwnershipError) and not mutation_started:
                _delete_private_file(
                    _journal_path(target),
                    limit=_JOURNAL_MAX_BYTES,
                )
                raise
            _recover_transaction(client, target, journal)
            if isinstance(error, IntegrationError):
                raise error
            raise IntegrationError("Codex integration removal failed") from None
        return IntegrationReport(
            True,
            MARKETPLACE_NAME,
            PLUGIN_SELECTOR,
            owned.runtime_version,
        )


def render_codex_config(*, marketplace_root: Path, runtime_version: str) -> str:
    """Render the supported read-only Codex CLI configuration preview."""
    source = Path(marketplace_root).expanduser().resolve()
    version = _receipt_for(source, runtime_version).runtime_version
    return (
        f"# Hydra for Codex {version}\n"
        f"codex plugin marketplace add {shlex.quote(str(source))}\n"
        f"codex plugin add {PLUGIN_SELECTOR}\n"
    )
