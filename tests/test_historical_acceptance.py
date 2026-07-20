from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import re
import tempfile
import unittest

from hydra_codex.rollout import Pseudonymizer, ingest_rollouts
from hydra_codex.storage import HydraStore
from hydra_codex.task_tree import (
    ActivityObservation,
    LifecycleObservation,
    NormalizedSession,
    TokenObservation,
    TokenVector,
    aggregate_task_tree,
)
from hydra_codex.task_tree_storage import aggregate_stored_task_tree


FIXTURES = Path(__file__).parent / "fixtures" / "historical"
THREAD_SUFFIX = re.compile(
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$",
    flags=re.IGNORECASE,
)


def timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def materialize_fixture(source: Path, destination: Path, project: Path) -> None:
    destination.mkdir(parents=True)
    for path in sorted(source.glob("*.jsonl")):
        target = destination / path.name
        with path.open("r", encoding="utf-8") as reader, target.open("w", encoding="utf-8") as writer:
            for line in reader:
                writer.write(line.replace("__PROJECT_ROOT__", str(project)))


def normalized_fixture_observations(root: Path):
    sessions: list[NormalizedSession] = []
    tokens: list[TokenObservation] = []
    lifecycle: list[LifecycleObservation] = []
    activities: list[ActivityObservation] = []
    for path in sorted(root.glob("*.jsonl")):
        match = THREAD_SUFFIX.search(path.stem)
        if match is None:
            raise AssertionError(f"fixture lacks source thread suffix: {path.name}")
        source_thread = match.group(1)
        source_meta_seen = False
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                envelope = json.loads(line)
                observed_at = timestamp(envelope["timestamp"])
                activities.append(ActivityObservation(source_thread, observed_at))
                payload = envelope["payload"]
                if envelope["type"] == "session_meta" and payload.get("id") == source_thread:
                    if source_meta_seen:
                        raise AssertionError(f"duplicate source metadata in {path.name}")
                    source_meta_seen = True
                    sessions.append(NormalizedSession(
                        source_thread, payload.get("parent_thread_id"), timestamp(payload["timestamp"]),
                    ))
                elif envelope["type"] == "event_msg" and payload.get("type") == "token_count":
                    usage = payload["info"]["total_token_usage"]
                    tokens.append(TokenObservation(
                        source_thread, observed_at, line_number,
                        TokenVector(
                            usage["input_tokens"], usage["cached_input_tokens"],
                            usage["output_tokens"], usage["reasoning_output_tokens"],
                        ),
                    ))
                elif envelope["type"] == "event_msg" and payload.get("type") == "task_complete":
                    lifecycle.append(LifecycleObservation(source_thread, "task_complete", observed_at))
        if not source_meta_seen:
            raise AssertionError(f"fixture lacks source metadata: {path.name}")
    return tuple(sessions), tuple(tokens), tuple(lifecycle), tuple(activities)


def nested_items(value):
    if isinstance(value, dict):
        for key, nested in value.items():
            yield key, nested
            yield from nested_items(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from nested_items(nested)


class HistoricalAcceptanceTests(unittest.TestCase):
    def test_privacy_reduced_fixtures_contain_only_normalized_acceptance_events(self) -> None:
        allowed_envelopes = {"session_meta", "event_msg"}
        allowed_events = {
            "token_count", "task_complete", "hydra_fixture_activity_boundary",
        }
        forbidden_keys = {
            "prompt", "message", "content", "arguments", "input", "output", "command",
            "tool_input", "tool_output", "username", "email", "credential", "secret",
        }
        def assert_private(key, value) -> None:
            self.assertNotIn(key.lower(), forbidden_keys)
            if not isinstance(value, str):
                return
            self.assertNotRegex(value, r"/(?:Users|home)/")
            self.assertNotRegex(value, r"\b[0-9a-f]{64}\b")
            for identifier in re.findall(
                r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                value, flags=re.IGNORECASE,
            ):
                self.assertTrue(identifier.startswith(("10000000-", "20000000-")))
        boundary_count = 0
        for path in FIXTURES.glob("*/*.jsonl"):
            with self.subTest(path=path.name), path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    envelope = json.loads(line)
                    self.assertIn(envelope["type"], allowed_envelopes)
                    for key, value in nested_items(envelope["payload"]):
                        assert_private(key, value)
                    if envelope["type"] == "event_msg":
                        self.assertIn(envelope["payload"]["type"], allowed_events)
                        boundary_count += envelope["payload"]["type"] == "hydra_fixture_activity_boundary"
        self.assertEqual(boundary_count, 51)
        for path in FIXTURES.glob("*-manifest.json"):
            with self.subTest(path=path.name):
                manifest = json.loads(path.read_text(encoding="utf-8"))
                for key, value in nested_items(manifest):
                    assert_private(key, value)

    def test_real_historical_rollouts_reconstruct_accepted_totals_without_session_manifest_vectors(self) -> None:
        for fixture_name in ("newer", "older"):
            with self.subTest(fixture=fixture_name), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary)
                project = base / "project"
                (project / ".hydra").mkdir(parents=True)
                project_id = f"historical-{fixture_name}"
                (project / ".hydra" / "project.toml").write_text(
                    f'project_id = "{project_id}"\n', encoding="utf-8",
                )
                materialized = base / "rollouts"
                materialize_fixture(FIXTURES / fixture_name, materialized, project)
                manifest = json.loads(
                    (FIXTURES / f"{fixture_name}-manifest.json").read_text(encoding="utf-8")
                )
                store = HydraStore(base / "hydra.sqlite3")
                self.addCleanup(store.close)

                report = ingest_rollouts(
                    store, (materialized,), project, project_id, hash_key=b"h" * 32,
                )
                observations = normalized_fixture_observations(materialized)
                raw_metrics = aggregate_task_tree(
                    root_id=manifest["root"], sessions=observations[0], tokens=observations[1],
                    lifecycle=observations[2], activities=observations[3],
                )
                hasher = Pseudonymizer(b"h" * 32)
                metrics = aggregate_stored_task_tree(
                    store.connection, project_id=project_id,
                    root_id=hasher.digest("identity", manifest["root"]),
                )

                expected = manifest["expected"]
                expected_baselines = (
                    manifest["observed_replay_baselines"], manifest["zero_no_observation"],
                )
                self.assertNotIn("sessions", manifest)
                self.assertEqual(manifest["session_count"], expected["sessions"])
                self.assertEqual(report.files_seen, expected["sessions"])
                self.assertEqual(store.count("rollout_sessions"), expected["sessions"])
                self.assertEqual(store.count("session_edges"), expected["sessions"] - 1)
                unknown_activity = store.connection.execute(
                    "SELECT COUNT(*) FROM rollout_diagnostics WHERE envelope_kind='unknown_event_type'"
                ).fetchone()[0]
                self.assertEqual(unknown_activity, expected["sessions"] - 1)
                expected_activity: dict[str, datetime] = {}
                for activity in observations[3]:
                    expected_activity[activity.session_id] = max(
                        expected_activity.get(activity.session_id, activity.observed_at),
                        activity.observed_at,
                    )
                for session_id, last_activity in expected_activity.items():
                    stored = store.connection.execute(
                        "SELECT last_activity_at FROM rollout_sessions WHERE session_key=?",
                        (hasher.digest("identity", session_id),),
                    ).fetchone()
                    self.assertIsNotNone(stored)
                    self.assertEqual(timestamp(stored[0]), last_activity)
                self.assertIn(hasher.digest("identity", manifest["root"]), metrics.session_ids)
                self.assertEqual(metrics.cutoff_at, timestamp(manifest["cutoff"]))
                self.assertEqual(metrics.sessions.value, expected["sessions"])
                self.assertEqual(metrics.subagents.value, expected["sessions"] - 1)
                self.assertEqual(metrics.unique.vector.input_tokens, expected["input_tokens"])
                self.assertEqual(metrics.unique.vector.cached_input_tokens, expected["cached_input_tokens"])
                self.assertEqual(metrics.unique.vector.output_tokens, expected["output_tokens"])
                self.assertEqual(
                    metrics.unique.vector.reasoning_output_tokens,
                    expected["reasoning_output_tokens"],
                )
                self.assertEqual(metrics.unique.working_tokens, expected["working_tokens"])
                self.assertEqual(metrics.unique.full_context, expected["full_context"])
                self.assertEqual(metrics.root_wall_clock_ms.value / 1000, manifest["wall_clock_seconds"])
                self.assertEqual(metrics.agent_time_ms.value / 1000, manifest["agent_time_seconds"])
                self.assertEqual(
                    (metrics.observed_replay_baselines, metrics.zero_no_observation),
                    expected_baselines,
                )
                self.assertEqual(metrics.semantic_coverage.value, manifest["semantic_coverage"])
                self.assertEqual(metrics.unique.provenance, "estimated")
                self.assertIn(
                    f"zero_no_observation:{expected_baselines[1]}",
                    metrics.unique.caveats,
                )
                self.assertEqual(metrics.recorded.vector, metrics.unique.vector + metrics.replay_baseline.vector)
                self.assertEqual(metrics.recorded.vector, raw_metrics.recorded.vector)
                self.assertEqual(metrics.replay_baseline.vector, raw_metrics.replay_baseline.vector)
                self.assertEqual(metrics.unique.vector, raw_metrics.unique.vector)
                self.assertEqual(metrics.root_wall_clock_ms, raw_metrics.root_wall_clock_ms)
                self.assertEqual(metrics.agent_time_ms, raw_metrics.agent_time_ms)
                self.assertEqual(metrics.semantic_coverage.value, 0)
                self.assertEqual(metrics.tool_calls.value, 0)
                self.assertEqual(metrics.file_reads.known_lower_bound, 0)


if __name__ == "__main__":
    unittest.main()
