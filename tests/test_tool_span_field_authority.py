from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from hydra_codex.storage import HydraStore
from hydra_codex.tool_spans import persist_tool_end, persist_tool_start


class ToolSpanFieldAuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary.name) / "hydra.sqlite3"
        self.store = HydraStore(self.database)
        self.connection = self.store.connection
        self.connection.execute(
            """INSERT INTO rollout_sessions(
                   session_key,project_id,path_key,resume_segments,conversation_key)
               VALUES ('session','project','path',1,'conversation')"""
        )
        for source, schema, family in (
            ("app-raw", "codex.app-server/v2", "app_server"),
            ("otel-raw", "codex.otel-log/v1", "otel"),
        ):
            self.connection.execute(
                """INSERT INTO codex_event_sources(
                       source_digest,project_id,schema_version,source_format,
                       line_count,byte_count)
                   VALUES (?,?,?,?,1,1)""",
                (source, "project", schema, family),
            )
        for source, source_type, chain in (
            ("rollout", "jsonl", "rollout-chain"),
            ("app", "explicit", "app-raw"),
            ("otel", "explicit", "otel-raw"),
        ):
            logical = f"{source}-logical"
            self.connection.execute(
                """INSERT INTO rollout_logical_sources(
                       logical_source_key,project_id,lineage_state)
                   VALUES (?,?,'clean')""",
                (logical, "project"),
            )
            self.connection.execute(
                """INSERT INTO rollout_sources(
                       source_digest,source_type,logical_source_key,relation,
                       line_count,byte_count,chain_digest,materialized)
                   VALUES (?,?,?,'initial',1,1,?,1)""",
                (source, source_type, logical, chain),
            )

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def _start(
        self, source: str, *, observed_at: str, ordinal: int, turn: str,
        call: str = "call",
    ) -> None:
        persist_tool_start(
            self.connection,
            session_key="session",
            call_key=call,
            category="tool",
            tool_name="exec_command",
            started_at=observed_at,
            turn_key=turn,
            source_digest=source,
            source_ordinal=ordinal,
        )

    def _end(
        self,
        source: str,
        *,
        observed_at: str,
        ordinal: int,
        terminal: str,
        latency: int | None,
        turn: str,
        call: str = "call",
    ) -> None:
        persist_tool_end(
            self.connection,
            session_key="session",
            call_key=call,
            category="tool",
            tool_name="exec_command",
            finished_at=observed_at,
            terminal_state=terminal,
            latency_ms=latency,
            turn_key=turn,
            source_digest=source,
            source_ordinal=ordinal,
        )

    def _row(self, call: str = "call") -> tuple[object, ...]:
        return tuple(self.connection.execute(
            """SELECT terminal_state,started_at,finished_at,latency_ms,
                      turn_key,source_digest,completeness
                 FROM tool_spans WHERE session_key='session' AND call_key=?""",
            (call,),
        ).fetchone())

    def test_app_terminal_fields_beat_otel_independent_of_ingest_order(self) -> None:
        observed: list[tuple[object, ...]] = []
        for index, lower_order in enumerate((("otel", "app"), ("app", "otel"))):
            call = f"terminal-{index}"
            self._start(
                "rollout", observed_at="2024-01-01T00:00:00.100000Z",
                ordinal=1, turn="rollout-turn", call=call,
            )
            facts = {
                "otel": dict(
                    observed_at="2024-01-01T00:00:00.500000Z", ordinal=2,
                    terminal="failed", latency=500, turn="otel-turn",
                ),
                "app": dict(
                    observed_at="2024-01-01T00:00:00.400000Z", ordinal=2,
                    terminal="success", latency=300, turn="app-turn",
                ),
            }
            for source in lower_order:
                self._end(source, call=call, **facts[source])
            observed.append(self._row(call))

        self.assertEqual(observed[0], observed[1])
        self.assertEqual(observed[0], (
            "success",
            "2024-01-01T00:00:00.100000Z",
            "2024-01-01T00:00:00.400000Z",
            300,
            "rollout-turn",
            "rollout",
            "complete",
        ))

    def test_each_missing_field_uses_its_highest_available_source(self) -> None:
        observed: list[tuple[object, ...]] = []
        for index, lower_order in enumerate((("otel", "app"), ("app", "otel"))):
            call = f"fields-{index}"
            # The rollout owns the terminal and finish, but has no start or
            # latency.  Those two fields must independently choose App over
            # OTel instead of whichever lower source happened to arrive first.
            self._end(
                "rollout", observed_at="2024-01-01T00:00:00.900000Z",
                ordinal=9, terminal="failed", latency=None,
                turn="rollout-turn", call=call,
            )
            starts = {
                "otel": dict(
                    observed_at="2024-01-01T00:00:00.050000Z", ordinal=1,
                    turn="otel-turn",
                ),
                "app": dict(
                    observed_at="2024-01-01T00:00:00.100000Z", ordinal=1,
                    turn="app-turn",
                ),
            }
            ends = {
                "otel": dict(
                    observed_at="2024-01-01T00:00:00.700000Z", ordinal=2,
                    terminal="success", latency=500, turn="otel-turn",
                ),
                "app": dict(
                    observed_at="2024-01-01T00:00:00.600000Z", ordinal=2,
                    terminal="success", latency=300, turn="app-turn",
                ),
            }
            for source in lower_order:
                self._start(source, call=call, **starts[source])
                self._end(source, call=call, **ends[source])
            observed.append(self._row(call))

        self.assertEqual(observed[0], observed[1])
        self.assertEqual(observed[0], (
            "failed",
            "2024-01-01T00:00:00.100000Z",
            "2024-01-01T00:00:00.900000Z",
            300,
            "rollout-turn",
            "rollout",
            "complete",
        ))

    def test_missing_timestamp_tool_candidate_preserves_estimated_provenance(self) -> None:
        persist_tool_end(
            self.connection,
            session_key="session",
            call_key="missing-time",
            category="tool",
            tool_name="exec_command",
            finished_at=None,
            terminal_state="success",
            latency_ms=None,
            turn_key="app-turn",
            source_digest="app",
            source_ordinal=4,
            provenance="estimated",
        )

        row = self.connection.execute(
            "SELECT terminal_state,provenance,completeness FROM tool_spans "
            "WHERE call_key='missing-time'",
        ).fetchone()
        self.assertEqual(tuple(row), ("success", "estimated", "incomplete"))


if __name__ == "__main__":
    unittest.main()
