from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from hydra_codex.rollout_identity import ACTIVE_HASHER, Pseudonymizer
from hydra_codex.rollout_persistence import persist_file
from hydra_codex.storage import HydraStore
from hydra_codex.tool_spans import persist_tool_end, persist_tool_start


def _successful_tool(connection: object) -> None:
    persist_tool_start(
        connection, session_key="session", call_key="shared-call",
        category="tool", tool_name="unknown",
        started_at="2026-07-20T00:00:00Z", turn_key="turn-a",
        source_digest="source", source_ordinal=0,
    )
    persist_tool_end(
        connection, session_key="session", call_key="shared-call",
        category="tool", tool_name="unknown",
        finished_at="2026-07-20T00:00:01Z", terminal_state="success",
        latency_ms=1000, turn_key="turn-a", source_digest="source",
        source_ordinal=1,
    )


class FileObservationOrderTests(unittest.TestCase):
    def test_equal_timestamp_turn_attribution_has_a_stable_tie_break(self) -> None:
        observed: list[str] = []
        for index, turns in enumerate((("turn-a", "turn-b"), ("turn-b", "turn-a"))):
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                store = HydraStore(root / f"hydra-{index}.sqlite3")
                self.addCleanup(store.close)
                token = ACTIVE_HASHER.set(Pseudonymizer(b"file-order-key-0000000000000001"))
                try:
                    with store.rollout_transaction() as connection:
                        _successful_tool(connection)
                        for turn in turns:
                            persist_file(
                                connection, "source", 1, "session", "read", "src/safe.py",
                                root, "2026-07-20T00:00:01Z", turn,
                                observation_call_key="shared-call",
                            )
                    observed.append(store.connection.execute(
                        "SELECT turn_key FROM file_observations",
                    ).fetchone()[0])
                finally:
                    ACTIVE_HASHER.reset(token)

        self.assertEqual(observed, ["turn-a", "turn-a"])

    def test_sub_millisecond_timestamp_order_does_not_use_sqlite_julianday(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = HydraStore(root / "hydra.sqlite3")
            self.addCleanup(store.close)
            token = ACTIVE_HASHER.set(Pseudonymizer(b"file-order-key-0000000000000001"))
            try:
                with store.rollout_transaction() as connection:
                    _successful_tool(connection)
                    persist_file(
                        connection, "source", 1, "session", "read", "src/safe.py",
                        root, "2026-07-20T00:00:01.000002Z", "turn-later",
                        observation_call_key="shared-call",
                    )
                    persist_file(
                        connection, "source", 2, "session", "read", "src/safe.py",
                        root, "2026-07-20T00:00:01.000001Z", "turn-earlier",
                        observation_call_key="shared-call",
                    )
                row = store.connection.execute(
                    "SELECT observed_at,turn_key FROM file_observations",
                ).fetchone()
            finally:
                ACTIVE_HASHER.reset(token)

        self.assertEqual(tuple(row), (
            "2026-07-20T00:00:01.000001Z", "turn-earlier",
        ))


if __name__ == "__main__":
    unittest.main()
