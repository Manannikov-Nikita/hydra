"""Versioned, read-only adapters for privacy-safe Codex event facts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Mapping

from .rollout_identity import Pseudonymizer
from .rollout_privacy import canonical_timestamp


APP_SERVER_V2 = "codex.app-server/v2"
OTEL_LOG_V1 = "codex.otel-log/v1"
_SCHEMAS = frozenset({APP_SERVER_V2, OTEL_LOG_V1})
_APP_METHODS = frozenset({
    "thread/started", "thread/archived", "thread/unarchived",
    "turn/started", "turn/completed", "thread/tokenUsage/updated",
    "item/started", "item/completed",
})
_OTEL_EVENTS = frozenset({
    "codex.conversation_starts", "codex.api_request", "codex.sse_event",
    "codex.websocket_request", "codex.websocket_event", "codex.user_prompt",
    "codex.tool_decision", "codex.tool_result",
})
_CONTENT_FIELDS = frozenset({
    "user_prompt", "assistant_message", "reasoning", "tool_input", "tool_output",
    "error_detail", "file_metadata",
})
_ISSUES = frozenset({
    "malformed_json", "invalid_encoding", "invalid_envelope", "unsupported_envelope",
    "invalid_timestamp", "invalid_attributes", "invalid_usage", "invalid_duration",
})


class EventAdapterError(ValueError):
    """The selected event boundary or local source is unsafe or unsupported."""


def _digest(key: bytes, domain: str, value: bytes) -> str:
    canonical_domain = "event" if domain == "event" else "diagnostic"
    return Pseudonymizer(key).digest(canonical_domain, f"{domain}/{value.hex()}")


def _canonical_bytes(value: Any) -> bytes:
    if isinstance(value, str):
        return value.encode("utf-8")
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


@dataclass(frozen=True)
class ContentFingerprint:
    field: str
    digest: str
    characters: int
    utf8_bytes: int

    def __post_init__(self) -> None:
        if self.field not in _CONTENT_FIELDS:
            raise ValueError("unsupported content field")
        if not isinstance(self.digest, str) or len(self.digest) != 64:
            raise ValueError("content digest must be sha256 hex")
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in (self.characters, self.utf8_bytes)):
            raise ValueError("content lengths must be non-negative integers")

    @classmethod
    def from_value(cls, field: str, value: Any, key: bytes) -> "ContentFingerprint":
        if not isinstance(key, bytes) or len(key) != 32:
            raise EventAdapterError("privacy key must be exactly 32 bytes")
        encoded = _canonical_bytes(value)
        characters = len(value) if isinstance(value, str) else len(encoded.decode("utf-8"))
        return cls(field, _digest(key, f"content:{field}", encoded), characters, len(encoded))


@dataclass(frozen=True)
class TokenSnapshotFact:
    counter_scope: str
    cumulative: bool
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    reasoning_tokens: int
    reported_total_tokens: int | None


@dataclass(frozen=True)
class ToolFact:
    call_key: str | None
    safe_name: str
    category: str
    phase: str
    status: str
    duration_ms: int | None
    exit_status: int | None

    @property
    def ephemeral_command(self) -> str | None:
        return getattr(self, "_ephemeral_command", None)

    @property
    def ephemeral_output(self) -> str | None:
        return getattr(self, "_ephemeral_output", None)

    @property
    def ephemeral_workdir(self) -> str | None:
        return getattr(self, "_ephemeral_workdir", None)

    @property
    def ephemeral_file_writes(self) -> tuple[str, ...]:
        return getattr(self, "_ephemeral_file_writes", ())


@dataclass(frozen=True)
class CodexEventFact:
    source_format: str
    schema_version: str
    source_ordinal: int
    event_key: str
    event_type: str
    observed_at: str | None
    observed_at_ns: int | None
    thread_key: str | None
    turn_key: str | None
    duration_ms: int | None
    status: str
    token_snapshots: tuple[TokenSnapshotFact, ...] = ()
    tool: ToolFact | None = None
    contents: tuple[ContentFingerprint, ...] = ()
    provenance: str = "exact"
    parent_thread_key: str | None = None
    child_thread_key: str | None = None


@dataclass(frozen=True)
class AdapterIssue:
    source_ordinal: int
    event_key: str
    code: str

    def __post_init__(self) -> None:
        if self.code not in _ISSUES:
            raise ValueError("unsupported adapter issue")


@dataclass(frozen=True)
class CodexEventBatch:
    schema: str
    events: tuple[CodexEventFact, ...]
    issues: tuple[AdapterIssue, ...]


def _opaque(key: bytes, domain: str, value: Any) -> str | None:
    canonical = {"thread": "identity", "turn": "turn", "call": "call"}
    return Pseudonymizer(key).digest(canonical[domain], value) if isinstance(value, str) and value else None


def _nonnegative(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _epoch_ns(value: int, divisor: int) -> tuple[str, int]:
    nanoseconds = value * divisor
    seconds, remainder = divmod(nanoseconds, 1_000_000_000)
    moment = datetime.fromtimestamp(seconds, timezone.utc).replace(microsecond=remainder // 1_000)
    text = moment.isoformat(timespec="microseconds").replace("+00:00", "Z")
    if text.endswith(".000000Z"):
        text = text.replace(".000000Z", "Z")
    return text, nanoseconds


def _safe_epoch_ns(value: int, divisor: int) -> tuple[str, int] | None:
    try:
        return _epoch_ns(value, divisor)
    except (OverflowError, OSError, ValueError):
        return None


def _app_timestamp(method: str, params: Mapping[str, Any]) -> tuple[str | None, int | None, bool]:
    millisecond_field = "startedAtMs" if method == "item/started" else "completedAtMs" if method == "item/completed" else None
    if millisecond_field is not None:
        value = _nonnegative(params.get(millisecond_field))
        converted = _safe_epoch_ns(value, 1_000_000) if value is not None else None
        return (*converted, False) if converted is not None else (None, None, True)
    turn = params.get("turn")
    if isinstance(turn, Mapping) and method in {"turn/started", "turn/completed"}:
        field = "startedAt" if method == "turn/started" else "completedAt"
        raw = turn.get(field)
        if raw is None:
            return None, None, False
        value = _nonnegative(raw)
        converted = _safe_epoch_ns(value, 1_000_000_000) if value is not None else None
        return (*converted, False) if converted is not None else (None, None, True)
    thread = params.get("thread")
    if method == "thread/started" and isinstance(thread, Mapping):
        value = _nonnegative(thread.get("createdAt"))
        converted = _safe_epoch_ns(value, 1_000_000_000) if value is not None else None
        return (*converted, False) if converted is not None else (None, None, True)
    return None, None, False


def _status(value: Any) -> str:
    if not isinstance(value, str):
        return "unknown"
    normalized = value.replace("inProgress", "in_progress")
    return normalized if normalized in {"in_progress", "completed", "failed", "interrupted", "cancelled", "declined", "success"} else "unknown"


def _content(field: str, value: Any, key: bytes) -> ContentFingerprint | None:
    if value is None or value == "" or value == [] or value == {}:
        return None
    try:
        return ContentFingerprint.from_value(field, value, key)
    except (TypeError, ValueError):
        return None


def _app_contents(item: Mapping[str, Any], turn: Mapping[str, Any] | None, key: bytes) -> tuple[ContentFingerprint, ...]:
    kind = item.get("type")
    candidates: list[tuple[str, Any]] = []
    if kind == "userMessage":
        candidates.append(("user_prompt", item.get("content")))
    elif kind in {"agentMessage", "plan"}:
        candidates.append(("assistant_message", item.get("text")))
    elif kind == "reasoning":
        candidates.extend((("reasoning", item.get("summary")), ("reasoning", item.get("content"))))
    elif kind == "commandExecution":
        candidates.extend((("tool_input", item.get("command")), ("tool_output", item.get("aggregatedOutput"))))
    elif kind in {"mcpToolCall", "dynamicToolCall"}:
        candidates.extend((("tool_input", item.get("arguments")), ("tool_output", item.get("result") or item.get("contentItems") or item.get("error"))))
    elif kind in {"collabToolCall", "collabAgentToolCall"}:
        candidates.append(("tool_input", item.get("prompt")))
    elif kind == "webSearch":
        candidates.append(("tool_input", item.get("query")))
    elif kind == "fileChange":
        candidates.append(("file_metadata", item.get("changes")))
    if turn is not None:
        candidates.append(("error_detail", turn.get("error")))
    return tuple(found for field, value in candidates if (found := _content(field, value, key)) is not None)


def _safe_tool(item: Mapping[str, Any], phase: str, key: bytes) -> ToolFact | None:
    kind = item.get("type")
    names = {
        "commandExecution": ("exec_command", "tool"), "fileChange": ("apply_patch", "tool"),
        "webSearch": ("web", "web"), "imageView": ("view_image", "tool"),
        "collabToolCall": ("collaboration", "tool"),
        "collabAgentToolCall": ("collaboration", "tool"),
        "dynamicToolCall": ("mcp", "tool"),
    }
    safe_name, category = names.get(kind, ("mcp", "tool"))
    if kind == "mcpToolCall":
        raw_name = item.get("tool")
        raw_server = item.get("server")
        if (isinstance(raw_name, str) and raw_name in {"hydra.annotate", "hydra.report"}) or raw_server == "hydra":
            safe_name, category = "hydra", "instrumentation"
    elif kind not in names:
        return None
    duration = _nonnegative(item.get("durationMs"))
    exit_status = item.get("exitCode")
    if not isinstance(exit_status, int) or isinstance(exit_status, bool):
        exit_status = None
    writes: tuple[str, ...] = ()
    changes = item.get("changes")
    if kind == "fileChange" and isinstance(changes, list) and all(
        isinstance(change, Mapping) and isinstance(change.get("path"), str)
        for change in changes
    ):
        writes = tuple(str(change["path"]) for change in changes)
    fact = ToolFact(
        _opaque(key, "call", item.get("id")), safe_name, category, phase,
        _status(item.get("status")), duration, exit_status,
    )
    object.__setattr__(
        fact, "_ephemeral_command",
        item.get("command")
        if kind == "commandExecution" and isinstance(item.get("command"), str)
        else None,
    )
    object.__setattr__(
        fact, "_ephemeral_output",
        item.get("aggregatedOutput")
        if kind == "commandExecution" and isinstance(item.get("aggregatedOutput"), str)
        else None,
    )
    object.__setattr__(
        fact, "_ephemeral_workdir",
        item.get("cwd")
        if kind == "commandExecution" and isinstance(item.get("cwd"), str)
        else None,
    )
    object.__setattr__(fact, "_ephemeral_file_writes", writes)
    return fact


def _usage(value: Any, scope: str, cumulative: bool) -> TokenSnapshotFact | None:
    if not isinstance(value, Mapping):
        return None
    names = ("inputTokens", "cachedInputTokens", "outputTokens", "reasoningOutputTokens")
    parsed = tuple(_nonnegative(value.get(name)) for name in names)
    if any(item is None for item in parsed) or parsed[1] > parsed[0]:
        return None
    total = value.get("totalTokens")
    if total is not None and _nonnegative(total) is None:
        return None
    return TokenSnapshotFact(scope, cumulative, parsed[0], parsed[1], parsed[2], parsed[3], total)


def _parse_app(envelope: Any, ordinal: int, event_key: str, key: bytes) -> tuple[CodexEventFact | None, tuple[str, ...]]:
    receipt_timestamp: tuple[str, int] | None = None
    receipt_invalid = False
    if isinstance(envelope, Mapping) and set(envelope) == {"received_at", "message"}:
        received = canonical_timestamp(envelope.get("received_at"))
        if received.text is None or received.epoch is None:
            receipt_invalid = True
        else:
            receipt_timestamp = (received.text, int(round(received.epoch * 1_000_000_000)))
        envelope = envelope.get("message")
    if not isinstance(envelope, Mapping) or set(envelope) - {"method", "params"} or not isinstance(envelope.get("params"), Mapping):
        return None, ("invalid_envelope",)
    method, params = envelope.get("method"), envelope["params"]
    if method not in _APP_METHODS:
        return None, ("unsupported_envelope",)
    thread_object = params.get("thread") if isinstance(params.get("thread"), Mapping) else None
    thread = params.get("threadId") if isinstance(params.get("threadId"), str) else thread_object.get("id") if thread_object else None
    turn = params.get("turn") if isinstance(params.get("turn"), Mapping) else None
    turn_id = params.get("turnId") if isinstance(params.get("turnId"), str) else turn.get("id") if turn else None
    if not isinstance(thread, str) or not thread:
        return None, ("invalid_envelope",)
    observed_at, observed_ns, bad_time = _app_timestamp(method, params)
    if receipt_timestamp is not None:
        observed_at, observed_ns = receipt_timestamp
    issues: list[str] = ["invalid_timestamp"] if receipt_invalid or (
        receipt_timestamp is None and bad_time
    ) else []
    item = params.get("item") if isinstance(params.get("item"), Mapping) else {}
    contents = _app_contents(item, turn, key)
    if thread_object is not None and (preview := _content("user_prompt", thread_object.get("preview"), key)) is not None:
        contents += (preview,)
    snapshots: tuple[TokenSnapshotFact, ...] = ()
    if method == "thread/tokenUsage/updated":
        token_usage = params.get("tokenUsage")
        if not isinstance(token_usage, Mapping):
            return None, ("invalid_usage",)
        total = _usage(token_usage.get("total"), "thread_total", True)
        last = _usage(token_usage.get("last"), "last_model_call", False)
        if total is None or last is None:
            return None, ("invalid_usage",)
        snapshots = (total, last)
    phase = "started" if method == "item/started" else "completed"
    tool = _safe_tool(item, phase, key) if method.startswith("item/") else None
    if tool is not None and item.get("durationMs") is not None and tool.duration_ms is None:
        issues.append("invalid_duration")
    duration = _nonnegative(turn.get("durationMs")) if turn else None
    if turn and turn.get("durationMs") is not None and duration is None:
        issues.append("invalid_duration")
    event_types = {"thread/tokenUsage/updated": "token_usage_updated"}
    event_type = event_types.get(method, method.replace("/", "_"))
    status = _status((turn or item).get("status"))
    collab = item.get("type") in {"collabToolCall", "collabAgentToolCall"}
    parent = item.get("senderThreadId") if collab else None
    # App Server uses receiverThreadId for messages and lifecycle operations on
    # an existing agent.  Only newThreadId is affirmative spawn-lineage proof.
    child = item.get("newThreadId") if collab else None
    return CodexEventFact(
        "app_server", APP_SERVER_V2, ordinal, event_key, event_type, observed_at, observed_ns,
        _opaque(key, "thread", thread), _opaque(key, "turn", turn_id), duration,
        status, snapshots, tool, contents, "exact" if observed_at is not None else "estimated",
        _opaque(key, "thread", parent), _opaque(key, "thread", child),
    ), tuple(issues)


def _otel_scalar(value: Any) -> str | int | float | bool | None:
    if not isinstance(value, Mapping) or len(value) != 1:
        return None
    field, scalar = next(iter(value.items()))
    if field == "stringValue" and isinstance(scalar, str):
        return scalar
    if field == "boolValue" and isinstance(scalar, bool):
        return scalar
    if field == "intValue" and isinstance(scalar, (str, int)) and not isinstance(scalar, bool):
        try:
            return int(scalar)
        except ValueError:
            return None
    if field == "doubleValue" and isinstance(scalar, (int, float)) and not isinstance(scalar, bool):
        return float(scalar)
    return None


def _otel_attributes(value: Any) -> dict[str, str | int | float | bool] | None:
    if not isinstance(value, list):
        return None
    result: dict[str, str | int | float | bool] = {}
    for item in value:
        if not isinstance(item, Mapping) or set(item) != {"key", "value"} or not isinstance(item.get("key"), str):
            return None
        scalar = _otel_scalar(item.get("value"))
        if scalar is None or item["key"] in result:
            return None
        result[item["key"]] = scalar
    return result


def _attr_int(attributes: Mapping[str, Any], *names: str) -> int | None:
    for name in names:
        if name in attributes:
            return _nonnegative(attributes[name])
    return None


def _otel_tool(attributes: Mapping[str, Any], event_name: str, key: bytes) -> ToolFact | None:
    if event_name not in {"codex.tool_decision", "codex.tool_result"}:
        return None
    raw_name = attributes.get("tool_name") or attributes.get("tool.name")
    if isinstance(raw_name, str) and raw_name in {"shell", "command_execution", "exec_command"}:
        safe_name, category = "exec_command", "tool"
    elif raw_name in {"apply_patch", "file_change"}:
        safe_name, category = "apply_patch", "tool"
    elif raw_name == "web_search":
        safe_name, category = "web", "web"
    elif isinstance(raw_name, str) and raw_name in {"hydra.annotate", "hydra.report"}:
        safe_name, category = "hydra", "instrumentation"
    else:
        safe_name, category = "mcp", "tool"
    success = attributes.get("success")
    status = "success" if success is True else "failed" if success is False else "unknown"
    return ToolFact(
        _opaque(key, "call", attributes.get("call.id") or attributes.get("call_id")),
        safe_name, category, "decision" if event_name.endswith("decision") else "completed",
        status, _attr_int(attributes, "duration_ms", "duration.ms"), None,
    )


def _parse_otel(envelope: Any, ordinal: int, event_key: str, key: bytes) -> tuple[CodexEventFact | None, tuple[str, ...]]:
    allowed = {"timeUnixNano", "observedTimeUnixNano", "severityNumber", "severityText", "body", "attributes", "traceId", "spanId", "flags", "droppedAttributesCount"}
    if not isinstance(envelope, Mapping) or set(envelope) - allowed:
        return None, ("invalid_envelope",)
    body = envelope.get("body")
    if not isinstance(body, Mapping) or set(body) != {"stringValue"} or not isinstance(body.get("stringValue"), str):
        return None, ("invalid_envelope",)
    event_name = body["stringValue"]
    if event_name not in _OTEL_EVENTS:
        return None, ("unsupported_envelope",)
    attributes = _otel_attributes(envelope.get("attributes"))
    if attributes is None:
        return None, ("invalid_attributes",)
    raw_time = envelope.get("timeUnixNano")
    observed_at = None
    observed_ns = None
    issues: list[str] = []
    try:
        numeric_time = int(raw_time) if isinstance(raw_time, (str, int)) and not isinstance(raw_time, bool) else -1
    except ValueError:
        numeric_time = -1
    if numeric_time < 0:
        issues.append("invalid_timestamp")
    else:
        converted = _safe_epoch_ns(numeric_time, 1)
        if converted is None:
            issues.append("invalid_timestamp")
        else:
            observed_at, observed_ns = converted
    event_kind = attributes.get("event.kind") or attributes.get("event_kind")
    snapshots: tuple[TokenSnapshotFact, ...] = ()
    if event_name in {"codex.sse_event", "codex.websocket_event"} and event_kind == "response.completed":
        usage = {
            "inputTokens": _attr_int(attributes, "input_tokens", "input.token.count"),
            "cachedInputTokens": _attr_int(attributes, "cached_input_tokens", "cached_input.token.count"),
            "outputTokens": _attr_int(attributes, "output_tokens", "output.token.count"),
            "reasoningOutputTokens": _attr_int(attributes, "reasoning_output_tokens", "reasoning_output.token.count"),
        }
        snapshot = _usage(usage, "model_call", False)
        if snapshot is None:
            issues.append("invalid_usage")
        else:
            snapshots = (snapshot,)
    content_keys = {
        "prompt": "user_prompt", "user_prompt": "user_prompt", "output": "tool_output",
        "output_snippet": "tool_output", "tool_input": "tool_input", "tool_output": "tool_output",
        "error": "error_detail", "error.message": "error_detail",
    }
    contents = tuple(
        found for raw, safe in content_keys.items()
        if raw in attributes and (found := _content(safe, attributes[raw], key)) is not None
    )
    tool = _otel_tool(attributes, event_name, key)
    duration = _attr_int(attributes, "duration_ms", "duration.ms")
    if any(name in attributes for name in ("duration_ms", "duration.ms")) and duration is None:
        issues.append("invalid_duration")
    status_value = attributes.get("status")
    if attributes.get("success") is True:
        status_value = "success"
    elif attributes.get("success") is False:
        status_value = "failed"
    event_type = {
        "codex.conversation_starts": "conversation_started", "codex.api_request": "api_request",
        "codex.user_prompt": "user_prompt", "codex.tool_decision": "tool_decision",
        "codex.tool_result": "tool_result",
    }.get(event_name, "sse_response_completed" if event_kind == "response.completed" else event_name.removeprefix("codex.").replace(".", "_"))
    return CodexEventFact(
        "otel", OTEL_LOG_V1, ordinal, event_key, event_type, observed_at, observed_ns,
        _opaque(key, "thread", attributes.get("conversation.id") or attributes.get("conversation_id")),
        _opaque(key, "turn", attributes.get("turn.id") or attributes.get("turn_id")),
        duration, _status(status_value), snapshots, tool, contents,
    ), tuple(issues)


def read_codex_event_jsonl(path: Path | str, *, schema: str, privacy_key: bytes) -> CodexEventBatch:
    """Read a local JSONL source without writing, sorting, or aggregating its facts."""
    if schema not in _SCHEMAS:
        raise EventAdapterError(f"unsupported event schema: {schema!r}")
    if not isinstance(privacy_key, bytes) or len(privacy_key) != 32:
        raise EventAdapterError("privacy key must be exactly 32 bytes")
    source = Path(path)
    if not source.is_file():
        raise EventAdapterError("event source must be a regular file")
    events: list[CodexEventFact] = []
    issues: list[AdapterIssue] = []
    parser = _parse_app if schema == APP_SERVER_V2 else _parse_otel
    with source.open("rb") as handle:
        for ordinal, raw_line in enumerate(handle, start=1):
            event_key = _digest(privacy_key, "event", raw_line)
            try:
                text = raw_line.decode("utf-8")
            except UnicodeDecodeError:
                issues.append(AdapterIssue(ordinal, event_key, "invalid_encoding"))
                continue
            try:
                envelope = json.loads(text)
            except json.JSONDecodeError:
                issues.append(AdapterIssue(ordinal, event_key, "malformed_json"))
                continue
            event, codes = parser(envelope, ordinal, event_key, privacy_key)
            if event is not None:
                events.append(event)
            issues.extend(AdapterIssue(ordinal, event_key, code) for code in codes)
    return CodexEventBatch(schema, tuple(events), tuple(issues))
