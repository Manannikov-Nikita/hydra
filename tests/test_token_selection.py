from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from hydra_codex.storage import HydraStore
from hydra_codex.token_selection import refresh_token_source_selection


PROJECT = "hprj_token_selection"


class TokenSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _store(self, name: str) -> HydraStore:
        return HydraStore(Path(self.temporary.name) / name)

    @staticmethod
    def _session(store: HydraStore, session_key: str) -> None:
        store.connection.execute(
            """INSERT INTO rollout_sessions(
                   session_key,project_id,path_key,resume_segments,
                   conversation_key)
               VALUES (?,?,'token-selection',1,?)""",
            (session_key, PROJECT, session_key),
        )

    @staticmethod
    def _token(
        store: HydraStore,
        *,
        source_digest: str,
        line_number: int,
        session_key: str,
        source_family: str,
        observed_at: str,
        input_tokens: int,
    ) -> None:
        store.connection.execute(
            """INSERT INTO token_snapshots(
                   source_digest,line_number,session_key,project_id,epoch,
                   input_tokens,cached_input_tokens,output_tokens,
                   reasoning_tokens,cache_write_tokens,completeness,
                   observed_at,source_family)
               VALUES (?,?,?,?,0,?,0,1,0,0,'complete',?,?)""",
            (
                source_digest,
                line_number,
                session_key,
                PROJECT,
                input_tokens,
                observed_at,
                source_family,
            ),
        )

    def _seed_mixed_sessions(self, store: HydraStore) -> tuple[str, ...]:
        sessions = ("session-rollout", "session-app", "session-otel")
        for session in sessions:
            self._session(store, session)
        for family, value in (("otel", 10), ("app_server", 20), ("rollout", 30)):
            self._token(
                store,
                source_digest=f"{family}-rollout-session",
                line_number=1,
                session_key="session-rollout",
                source_family=family,
                observed_at=f"2026-07-30T00:00:{value:02d}Z",
                input_tokens=value,
            )
        self._token(
            store,
            source_digest="otel-app-session",
            line_number=1,
            session_key="session-app",
            source_family="otel",
            observed_at="2026-07-30T00:01:00Z",
            input_tokens=50,
        )
        for line_number, value in ((1, 100), (2, 150)):
            self._token(
                store,
                source_digest="app-app-session",
                line_number=line_number,
                session_key="session-app",
                source_family="app_server",
                observed_at=f"2026-07-30T00:01:0{line_number}Z",
                input_tokens=value,
            )
        self._token(
            store,
            source_digest="otel-only-session",
            line_number=1,
            session_key="session-otel",
            source_family="otel",
            observed_at="2026-07-30T00:02:00Z",
            input_tokens=70,
        )
        return sessions

    @staticmethod
    def _selection_rows(store: HydraStore) -> tuple[tuple[object, ...], ...]:
        return tuple(
            tuple(row)
            for row in store.connection.execute(
                """SELECT source_digest,line_number,contributes_total,
                          selection_provenance,selection_caveat
                     FROM token_snapshots
                    ORDER BY source_digest,line_number""",
            )
        )

    def test_session_refreshes_are_equivalent_to_one_full_project_refresh(self) -> None:
        from hydra_codex.token_selection import refresh_token_session_selection

        full = self._store("full.sqlite3")
        scoped = self._store("scoped.sqlite3")
        self.addCleanup(full.close)
        self.addCleanup(scoped.close)
        sessions = self._seed_mixed_sessions(full)
        self._seed_mixed_sessions(scoped)

        refresh_token_source_selection(full.connection, PROJECT)
        for session in reversed(sessions):
            refresh_token_session_selection(scoped.connection, PROJECT, session)

        self.assertEqual(
            self._selection_rows(scoped),
            self._selection_rows(full),
        )

    def test_session_refresh_does_not_rewrite_an_unrelated_session(self) -> None:
        from hydra_codex.token_selection import refresh_token_session_selection

        store = self._store("isolation.sqlite3")
        self.addCleanup(store.close)
        self._seed_mixed_sessions(store)
        refresh_token_source_selection(store.connection, PROJECT)
        before = tuple(
            tuple(row)
            for row in store.connection.execute(
                """SELECT source_digest,line_number,contributes_total,
                          selection_provenance,selection_caveat
                     FROM token_snapshots
                    WHERE session_key='session-app'
                    ORDER BY source_digest,line_number""",
            )
        )
        self._token(
            store,
            source_digest="new-rollout-session",
            line_number=1,
            session_key="session-rollout",
            source_family="rollout",
            observed_at="2026-07-30T00:03:00Z",
            input_tokens=40,
        )

        refresh_token_session_selection(
            store.connection,
            PROJECT,
            "session-rollout",
        )

        after = tuple(
            tuple(row)
            for row in store.connection.execute(
                """SELECT source_digest,line_number,contributes_total,
                          selection_provenance,selection_caveat
                     FROM token_snapshots
                    WHERE session_key='session-app'
                    ORDER BY source_digest,line_number""",
            )
        )
        self.assertEqual(after, before)

    def test_session_refresh_work_is_bounded_by_the_selected_session(self) -> None:
        from hydra_codex.token_selection import refresh_token_session_selection

        store = self._store("bounded.sqlite3")
        self.addCleanup(store.close)
        self._session(store, "target-session")
        self._session(store, "unrelated-session")
        for line_number, family in ((1, "otel"), (2, "rollout")):
            self._token(
                store,
                source_digest="target-source",
                line_number=line_number,
                session_key="target-session",
                source_family=family,
                observed_at=f"2026-07-30T00:00:0{line_number}Z",
                input_tokens=line_number * 10,
            )

        def measured_refresh() -> int:
            operations = 0

            def progress() -> int:
                nonlocal operations
                operations += 1
                return 0

            store.connection.set_progress_handler(progress, 1)
            try:
                refresh_token_session_selection(
                    store.connection,
                    PROJECT,
                    "target-session",
                )
            finally:
                store.connection.set_progress_handler(None, 0)
            return operations

        baseline = measured_refresh()
        store.connection.executemany(
            """INSERT INTO token_snapshots(
                   source_digest,line_number,session_key,project_id,epoch,
                   input_tokens,cached_input_tokens,output_tokens,
                   reasoning_tokens,cache_write_tokens,completeness,
                   observed_at,source_family)
               VALUES ('unrelated-source',?,'unrelated-session',?,0,?,
                       0,1,0,0,'complete','2026-07-30T00:04:00Z','rollout')""",
            ((line_number, PROJECT, line_number) for line_number in range(1, 5001)),
        )

        expanded = measured_refresh()

        self.assertLess(expanded, 5_000)
        self.assertLessEqual(expanded, baseline * 3)


if __name__ == "__main__":
    unittest.main()
