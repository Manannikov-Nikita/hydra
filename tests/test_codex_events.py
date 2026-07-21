from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import tempfile
import unittest

from hydra_codex.codex_events import (
    APP_SERVER_V2,
    OTEL_LOG_V1,
    ContentFingerprint,
    EventAdapterError,
    read_codex_event_jsonl,
)
from hydra_codex.rollout_identity import Pseudonymizer


FIXTURES = Path(__file__).parent / "fixtures" / "codex_events"
KEY = b"event-adapter-fixture-key-000001"


class CodexEventAdapterTests(unittest.TestCase):
    def test_app_server_v2_normalizes_exact_facts_without_raw_content(self) -> None:
        source = FIXTURES / "app_server_v2.jsonl"
        original = source.read_bytes()

        batch = read_codex_event_jsonl(source, schema=APP_SERVER_V2, privacy_key=KEY)

        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(batch.schema, APP_SERVER_V2)
        self.assertEqual(batch.issues, ())
        self.assertEqual([event.source_ordinal for event in batch.events], [1, 2, 3, 4, 5, 6])
        self.assertTrue(all(event.provenance == "exact" for event in batch.events))
        self.assertNotEqual(batch.events[0].thread_key, "fixture-thread-a")
        self.assertNotEqual(batch.events[0].turn_key, "fixture-turn-a")
        canonical = Pseudonymizer(KEY)
        self.assertEqual(batch.events[0].thread_key, canonical.digest("identity", "fixture-thread-a"))
        self.assertEqual(batch.events[0].turn_key, canonical.digest("turn", "fixture-turn-a"))

        started, completed = batch.events[1:3]
        self.assertEqual(started.observed_at, "2024-07-03T09:46:41.200000Z")
        self.assertEqual(completed.observed_at, "2024-07-03T09:46:41.500000Z")
        self.assertEqual(started.tool.phase, "started")
        self.assertEqual(completed.tool.phase, "completed")
        self.assertEqual(completed.tool.safe_name, "exec_command")
        self.assertEqual(completed.tool.duration_ms, 300)
        self.assertEqual(completed.tool.exit_status, 0)
        self.assertEqual(completed.tool.call_key, started.tool.call_key)
        self.assertEqual(completed.tool.call_key, canonical.digest("call", "fixture-call-a"))

        usage = batch.events[3]
        self.assertEqual(len(usage.token_snapshots), 2)
        total, last = usage.token_snapshots
        self.assertEqual((total.counter_scope, total.cumulative), ("thread_total", True))
        self.assertEqual((total.input_tokens, total.cached_input_tokens, total.output_tokens), (100, 40, 30))
        self.assertEqual(total.reasoning_tokens, 5)
        self.assertEqual((last.counter_scope, last.cumulative), ("last_model_call", False))

        command_contents = {item.field: item for item in completed.contents}
        self.assertEqual(set(command_contents), {"tool_input", "tool_output"})
        self.assertEqual(command_contents["tool_output"].characters, len("fixture test output"))
        message = batch.events[4]
        self.assertEqual(message.observed_at, "2024-07-03T09:46:41.400000Z")
        self.assertEqual(message.contents[0].field, "assistant_message")

        serialized = json.dumps(asdict(batch), sort_keys=True)
        for raw in (
            "fixture-thread-a", "fixture-turn-a", "fixture-call-a",
            "python -m unittest", "fixture test output", "anonymized assistant fixture",
            "/fixture/project",
        ):
            self.assertNotIn(raw, serialized)

    def test_otel_v1_preserves_source_order_and_model_call_usage(self) -> None:
        batch = read_codex_event_jsonl(
            FIXTURES / "otel_log_v1.jsonl", schema=OTEL_LOG_V1, privacy_key=KEY,
        )

        self.assertEqual(batch.issues, ())
        self.assertEqual([event.source_ordinal for event in batch.events], [1, 2, 3, 4])
        self.assertEqual(
            [event.observed_at for event in batch.events],
            [
                "2024-07-03T09:46:40Z",
                "2024-07-03T09:46:41Z",
                "2024-07-03T09:46:40.900000Z",
                "2024-07-03T09:46:42Z",
            ],
        )
        token_event = batch.events[2]
        self.assertEqual(token_event.event_type, "sse_response_completed")
        self.assertEqual(len(token_event.token_snapshots), 1)
        snapshot = token_event.token_snapshots[0]
        self.assertEqual((snapshot.counter_scope, snapshot.cumulative), ("model_call", False))
        self.assertEqual(
            (snapshot.input_tokens, snapshot.cached_input_tokens, snapshot.output_tokens, snapshot.reasoning_tokens),
            (90, 30, 20, 4),
        )

        api_request = batch.events[1]
        self.assertEqual(api_request.duration_ms, 250)
        tool_result = batch.events[3]
        self.assertEqual((tool_result.tool.safe_name, tool_result.tool.phase), ("exec_command", "completed"))
        self.assertEqual(tool_result.tool.duration_ms, 40)
        self.assertEqual(tool_result.contents[0].field, "tool_output")

        serialized = json.dumps(asdict(batch), sort_keys=True)
        for raw in (
            "fixture-thread-b", "fixture-turn-b", "fixture-call-b",
            "anonymized user fixture", "fixture tool output",
        ):
            self.assertNotIn(raw, serialized)

    def test_multiple_cumulative_and_out_of_order_events_are_not_aggregated_or_sorted(self) -> None:
        lines = [
            {
                "method": "thread/tokenUsage/updated",
                "params": {
                    "threadId": "thread", "turnId": "turn",
                    "tokenUsage": {
                        "total": {"inputTokens": 100, "cachedInputTokens": 10, "outputTokens": 20, "reasoningOutputTokens": 3, "totalTokens": 120},
                        "last": {"inputTokens": 100, "cachedInputTokens": 10, "outputTokens": 20, "reasoningOutputTokens": 3, "totalTokens": 120},
                    },
                },
            },
            {
                "method": "item/completed",
                "params": {"threadId": "thread", "turnId": "turn", "completedAtMs": 1000, "item": {"id": "m", "type": "agentMessage", "text": "later in file"}},
            },
            {
                "method": "thread/tokenUsage/updated",
                "params": {
                    "threadId": "thread", "turnId": "turn",
                    "tokenUsage": {
                        "total": {"inputTokens": 40, "cachedInputTokens": 5, "outputTokens": 8, "reasoningOutputTokens": 1, "totalTokens": 48},
                        "last": {"inputTokens": 40, "cachedInputTokens": 5, "outputTokens": 8, "reasoningOutputTokens": 1, "totalTokens": 48},
                    },
                },
            },
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
            batch = read_codex_event_jsonl(path, schema=APP_SERVER_V2, privacy_key=KEY)

        self.assertEqual([event.source_ordinal for event in batch.events], [1, 2, 3])
        totals = [event.token_snapshots[0].input_tokens for event in batch.events if event.token_snapshots]
        self.assertEqual(totals, [100, 40])

    def test_app_server_thread_started_uses_v2_thread_shape_without_preview_leak(self) -> None:
        event = {
            "method": "thread/started",
            "params": {
                "thread": {
                    "id": "actual-v2-thread", "createdAt": 1720000000,
                    "preview": "private first prompt", "cwd": "/private/project",
                },
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            path.write_text(json.dumps(event) + "\n", encoding="utf-8")
            batch = read_codex_event_jsonl(path, schema=APP_SERVER_V2, privacy_key=KEY)

        self.assertEqual(batch.issues, ())
        self.assertEqual(len(batch.events), 1)
        fact = batch.events[0]
        self.assertEqual(fact.observed_at, "2024-07-03T09:46:40Z")
        self.assertIsNotNone(fact.thread_key)
        self.assertEqual(fact.contents[0].field, "user_prompt")
        serialized = json.dumps(asdict(batch), sort_keys=True)
        self.assertNotIn("actual-v2-thread", serialized)
        self.assertNotIn("private first prompt", serialized)
        self.assertNotIn("/private/project", serialized)

    def test_content_fingerprints_are_keyed_stable_and_field_scoped(self) -> None:
        first = ContentFingerprint.from_value("tool_input", "same", KEY)
        second = ContentFingerprint.from_value("tool_input", "same", KEY)
        other_field = ContentFingerprint.from_value("tool_output", "same", KEY)

        self.assertEqual(first, second)
        self.assertNotEqual(first.digest, other_field.digest)
        self.assertEqual((first.characters, first.utf8_bytes), (4, 4))
        self.assertNotIn("same", repr(first))

    def test_schema_key_and_path_validation_fail_closed(self) -> None:
        source = FIXTURES / "app_server_v2.jsonl"
        with self.assertRaisesRegex(EventAdapterError, "unsupported event schema"):
            read_codex_event_jsonl(source, schema="codex.app-server/v3", privacy_key=KEY)
        with self.assertRaisesRegex(EventAdapterError, "32 bytes"):
            read_codex_event_jsonl(source, schema=APP_SERVER_V2, privacy_key=b"short")
        with self.assertRaisesRegex(EventAdapterError, "regular file"):
            read_codex_event_jsonl(source.parent, schema=APP_SERVER_V2, privacy_key=KEY)

    def test_malformed_unknown_and_invalid_values_emit_safe_issues(self) -> None:
        lines = [
            "not json",
            json.dumps({"method": "item/agentMessage/delta", "params": {"delta": "private delta"}}),
            json.dumps({"method": "thread/tokenUsage/updated", "params": {"threadId": "private-thread", "turnId": "private-turn", "tokenUsage": {"total": {"inputTokens": -1}}}}),
            json.dumps({"timeUnixNano": "not-a-time", "body": {"stringValue": "codex.user_prompt"}, "attributes": [{"key": "prompt", "value": {"stringValue": "private prompt"}}]}),
        ]
        with tempfile.TemporaryDirectory() as directory:
            app_path = Path(directory) / "app.jsonl"
            app_path.write_text("\n".join(lines[:3]) + "\n", encoding="utf-8")
            app = read_codex_event_jsonl(app_path, schema=APP_SERVER_V2, privacy_key=KEY)
            otel_path = Path(directory) / "otel.jsonl"
            otel_path.write_text(lines[3] + "\n", encoding="utf-8")
            otel = read_codex_event_jsonl(otel_path, schema=OTEL_LOG_V1, privacy_key=KEY)

        self.assertEqual([issue.code for issue in app.issues], ["malformed_json", "unsupported_envelope", "invalid_usage"])
        self.assertEqual([issue.code for issue in otel.issues], ["invalid_timestamp"])
        serialized = json.dumps(asdict(app), sort_keys=True) + json.dumps(asdict(otel), sort_keys=True)
        for raw in ("private delta", "private-thread", "private-turn", "private prompt"):
            self.assertNotIn(raw, serialized)

    def test_top_level_shapes_and_otel_attributes_are_allowlisted(self) -> None:
        inputs = [
            [],
            {"method": "turn/started", "params": "not-an-object"},
            {"timeUnixNano": "1720000000000000000", "body": "codex.api_request", "attributes": []},
            {"timeUnixNano": "1720000000000000000", "body": {"stringValue": "codex.api_request"}, "attributes": {"secret": "not-standard-otlp"}},
        ]
        with tempfile.TemporaryDirectory() as directory:
            app_path = Path(directory) / "app.jsonl"
            app_path.write_text("\n".join(json.dumps(item) for item in inputs[:2]) + "\n", encoding="utf-8")
            app = read_codex_event_jsonl(app_path, schema=APP_SERVER_V2, privacy_key=KEY)
            otel_path = Path(directory) / "otel.jsonl"
            otel_path.write_text("\n".join(json.dumps(item) for item in inputs[2:]) + "\n", encoding="utf-8")
            otel = read_codex_event_jsonl(otel_path, schema=OTEL_LOG_V1, privacy_key=KEY)

        self.assertEqual([issue.code for issue in app.issues], ["invalid_envelope", "invalid_envelope"])
        self.assertEqual([issue.code for issue in otel.issues], ["invalid_envelope", "invalid_attributes"])

    def test_huge_timestamps_and_duplicate_otel_attributes_fail_closed_without_aborting(self) -> None:
        huge = 10**100
        app_lines = [
            {"method": "item/completed", "params": {"threadId": "thread", "turnId": "turn", "completedAtMs": huge, "item": {"id": "message", "type": "agentMessage", "text": "private app content"}}},
            {"method": "turn/started", "params": {"threadId": "thread", "turn": {"id": "turn", "items": [], "startedAt": 1720000000, "status": "inProgress"}}},
        ]
        duplicate = {
            "timeUnixNano": str(huge), "body": {"stringValue": "codex.user_prompt"},
            "attributes": [
                {"key": "prompt", "value": {"stringValue": "private first"}},
                {"key": "prompt", "value": {"stringValue": "private second"}},
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            app_path = Path(directory) / "app.jsonl"
            app_path.write_text("\n".join(json.dumps(item) for item in app_lines) + "\n", encoding="utf-8")
            app = read_codex_event_jsonl(app_path, schema=APP_SERVER_V2, privacy_key=KEY)
            otel_path = Path(directory) / "otel.jsonl"
            otel_path.write_text(json.dumps(duplicate) + "\n", encoding="utf-8")
            otel = read_codex_event_jsonl(otel_path, schema=OTEL_LOG_V1, privacy_key=KEY)

        self.assertEqual([issue.code for issue in app.issues], ["invalid_timestamp"])
        self.assertEqual([event.source_ordinal for event in app.events], [1, 2])
        self.assertIsNone(app.events[0].observed_at)
        self.assertEqual([issue.code for issue in otel.issues], ["invalid_attributes"])
        serialized = json.dumps(asdict(app), sort_keys=True) + json.dumps(asdict(otel), sort_keys=True)
        for raw in ("private app content", "private first", "private second"):
            self.assertNotIn(raw, serialized)


if __name__ == "__main__":
    unittest.main()
