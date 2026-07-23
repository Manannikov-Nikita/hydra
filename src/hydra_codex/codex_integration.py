"""Owned, idempotent reconciliation of Hydra's supported Codex plugin state."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import selectors
import shlex
import shutil
import stat
import subprocess
import tempfile
import time
from typing import Mapping, Protocol


MARKETPLACE_NAME = "hydra"
PLUGIN_NAME = "hydra-codex"
PLUGIN_SELECTOR = "hydra-codex@hydra"
RECEIPT_SCHEMA_VERSION = 1
_RECEIPT_MAX_BYTES = 16 * 1024
_COMMAND_MAX_BYTES = 1024 * 1024
_SAFE_PATH_PARTS = (
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "/usr/bin",
    "/bin",
    "/usr/sbin",
    "/sbin",
)


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
        resolved = shutil.which(executable, path=values.get("PATH", ""))
        if resolved is None:
            raise IncompatibleCodexError("Codex integration is unavailable")
        self._executable = str(Path(resolved).resolve())
        self._timeout = timeout
        self._max_output_bytes = max_output_bytes
        allowed = (
            "HOME",
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

    def _json(self, arguments: list[str]) -> object:
        raw = self._run(arguments)
        try:
            return json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            raise IncompatibleCodexError(
                "Codex integration is unavailable",
            ) from None

    def _mutation(self, arguments: list[str]) -> None:
        if not isinstance(self._json(arguments), Mapping):
            raise IncompatibleCodexError("Codex integration is unavailable")

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
        payload = self._json(["plugin", "marketplace", "list", "--json"])
        if not isinstance(payload, list):
            raise IncompatibleCodexError("Codex integration is unavailable")
        records: list[MarketplaceRecord] = []
        for item in payload:
            if (
                not isinstance(item, Mapping)
                or not isinstance(item.get("name"), str)
                or not item["name"]
                or not isinstance(item.get("source"), str)
                or not item["source"]
                or not Path(item["source"]).is_absolute()
            ):
                raise IncompatibleCodexError("Codex integration is unavailable")
            records.append(
                MarketplaceRecord(
                    item["name"],
                    Path(item["source"]).expanduser().resolve(),
                ),
            )
        return tuple(records)

    def add_marketplace(self, root: Path) -> None:
        self._mutation([
            "plugin", "marketplace", "add", str(root), "--json",
        ])

    def remove_marketplace(self, name: str) -> None:
        self._mutation([
            "plugin", "marketplace", "remove", name, "--json",
        ])

    def list_plugins(
        self,
        marketplace: str,
        *,
        include_available: bool,
    ) -> tuple[PluginRecord, ...]:
        arguments = ["plugin", "list", "--marketplace", marketplace]
        if include_available:
            arguments.append("--available")
        arguments.append("--json")
        payload = self._json(arguments)
        if not isinstance(payload, list):
            raise IncompatibleCodexError("Codex integration is unavailable")
        records: list[PluginRecord] = []
        for item in payload:
            version = item.get("version") if isinstance(item, Mapping) else object()
            if (
                not isinstance(item, Mapping)
                or not isinstance(item.get("name"), str)
                or not item["name"]
                or not isinstance(item.get("marketplace"), str)
                or not item["marketplace"]
                or not isinstance(item.get("installed"), bool)
                or not (version is None or isinstance(version, str) and version)
            ):
                raise IncompatibleCodexError("Codex integration is unavailable")
            records.append(
                PluginRecord(
                    item["name"],
                    item["marketplace"],
                    item["installed"],
                    version,
                ),
            )
        return tuple(records)

    def add_plugin(self, selector: str) -> None:
        self._mutation(["plugin", "add", selector, "--json"])

    def remove_plugin(self, selector: str) -> None:
        self._mutation(["plugin", "remove", selector, "--json"])


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
    return None if not matches else matches[0]


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
    if (
        not isinstance(runtime_version, str)
        or not runtime_version
        or len(runtime_version.encode("utf-8")) > 256
        or any(character in runtime_version for character in "\r\n\0")
    ):
        raise IntegrationError("Hydra runtime version is invalid")
    return _Receipt(
        MARKETPLACE_NAME,
        source,
        PLUGIN_SELECTOR,
        runtime_version,
    )


def _read_receipt(path: Path) -> tuple[_Receipt | None, bytes | None]:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return None, None
    except OSError:
        raise IntegrationOwnershipError(
            "Codex integration ownership is ambiguous",
        ) from None
    if not stat.S_ISREG(mode) or os.name == "posix" and mode & 0o077:
        raise IntegrationOwnershipError(
            "Codex integration ownership is ambiguous",
        )
    try:
        with path.open("rb") as stream:
            raw = stream.read(_RECEIPT_MAX_BYTES + 1)
    except OSError:
        raise IntegrationOwnershipError(
            "Codex integration ownership is ambiguous",
        ) from None
    if len(raw) > _RECEIPT_MAX_BYTES:
        raise IntegrationOwnershipError(
            "Codex integration ownership is ambiguous",
        )
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise IntegrationOwnershipError(
            "Codex integration ownership is ambiguous",
        ) from None
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
        or not isinstance(payload.get("runtime_version"), str)
        or not payload["runtime_version"]
        or not isinstance(payload.get("source"), str)
        or not Path(payload["source"]).is_absolute()
    ):
        raise IntegrationOwnershipError(
            "Codex integration ownership is ambiguous",
        )
    return (
        _Receipt(
            MARKETPLACE_NAME,
            Path(payload["source"]).resolve(),
            PLUGIN_SELECTOR,
            payload["runtime_version"],
        ),
        raw,
    )


def _write_receipt_bytes(path: Path, content: bytes) -> None:
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix":
        os.chmod(parent, 0o700)
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
        directory = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
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


def _write_receipt(path: Path, receipt: _Receipt) -> None:
    content = (
        json.dumps(
            receipt.payload(),
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    _write_receipt_bytes(path, content)


def _delete_receipt(path: Path) -> None:
    mode = path.lstat().st_mode
    if not stat.S_ISREG(mode):
        raise IntegrationOwnershipError(
            "Codex integration ownership is ambiguous",
        )
    path.unlink()
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


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


def _clear_state(client: CodexClient) -> None:
    state = inspect_codex(client)
    if state.plugin is not None and state.plugin.installed:
        client.remove_plugin(PLUGIN_SELECTOR)
    if state.marketplace is not None:
        client.remove_marketplace(MARKETPLACE_NAME)


def _restore_state(client: CodexClient, previous: CodexState) -> None:
    _clear_state(client)
    if previous.marketplace is not None:
        client.add_marketplace(previous.marketplace.source)
    if previous.plugin is not None and previous.plugin.installed:
        client.add_plugin(PLUGIN_SELECTOR)
    restored = inspect_codex(client)
    if previous.marketplace is None:
        if restored.marketplace is not None or (
            restored.plugin is not None and restored.plugin.installed
        ):
            raise IntegrationError("Codex integration rollback failed")
        return
    if (
        restored.marketplace is None
        or restored.marketplace.source != previous.marketplace.source
        or bool(restored.plugin and restored.plugin.installed)
        != bool(previous.plugin and previous.plugin.installed)
        or (
            previous.plugin is not None
            and previous.plugin.installed
            and (
                restored.plugin is None
                or restored.plugin.version != previous.plugin.version
            )
        )
    ):
        raise IntegrationError("Codex integration rollback failed")


def _available_version(client: CodexClient) -> str | None:
    plugin = _one_plugin(
        client.list_plugins(MARKETPLACE_NAME, include_available=True),
    )
    return None if plugin is None else plugin.version


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
    owned, original_receipt = _read_receipt(receipt_target)
    current = inspect_codex(client)
    _ensure_owned(current, owned)
    if _state_is_exact(current, desired) and owned == desired and not refresh:
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
                or refresh
            )
        )
        replace_plugin = bool(
            current.plugin is not None
            and current.plugin.installed
            and (
                current.plugin.version != runtime_version
                or replace_marketplace
                or refresh
            )
        )
        if replace_plugin:
            client.remove_plugin(PLUGIN_SELECTOR)
        if replace_marketplace:
            client.remove_marketplace(MARKETPLACE_NAME)
        if current.marketplace is None or replace_marketplace:
            client.add_marketplace(source)
        if _available_version(client) != runtime_version:
            raise IntegrationError(
                "Bundled plugin version does not match Hydra runtime",
            )
        state_before_plugin = inspect_codex(client)
        if state_before_plugin.plugin is None or not state_before_plugin.plugin.installed:
            client.add_plugin(PLUGIN_SELECTOR)
        verified = inspect_codex(client)
        if not _state_is_exact(verified, desired):
            raise IntegrationError("Codex integration verification failed")
        _write_receipt(receipt_target, desired)
    except Exception as error:
        try:
            _restore_state(client, current)
            if original_receipt is not None:
                _write_receipt_bytes(receipt_target, original_receipt)
            elif receipt_target.exists():
                _delete_receipt(receipt_target)
        except Exception:
            raise IntegrationError(
                "Codex integration update and rollback failed",
            ) from None
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
    owned, original_receipt = _read_receipt(target)
    current = inspect_codex(client)
    _ensure_owned(current, owned)
    if owned is None:
        return IntegrationReport(False, MARKETPLACE_NAME, PLUGIN_SELECTOR, "")
    try:
        if current.plugin is not None and current.plugin.installed:
            client.remove_plugin(PLUGIN_SELECTOR)
        if current.marketplace is not None:
            client.remove_marketplace(MARKETPLACE_NAME)
        removed = inspect_codex(client)
        if removed.marketplace is not None or bool(
            removed.plugin is not None and removed.plugin.installed
        ):
            raise IntegrationError("Codex integration removal verification failed")
        _delete_receipt(target)
    except Exception as error:
        try:
            _restore_state(client, current)
            if original_receipt is not None:
                _write_receipt_bytes(target, original_receipt)
        except Exception:
            raise IntegrationError(
                "Codex integration removal and rollback failed",
            ) from None
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
        f"codex plugin marketplace add {shlex.quote(str(source))} --json\n"
        f"codex plugin add {PLUGIN_SELECTOR} --json\n"
    )
