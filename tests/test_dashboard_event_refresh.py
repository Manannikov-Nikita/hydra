from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from hydra_codex.codex_event_ingest import CodexEventSource, ingest_codex_events
from hydra_codex.codex_events import APP_SERVER_V2, EventAdapterError
from hydra_codex.dashboard_refresh import (
    GlobalRefreshRunner,
    GlobalRolloutPlan,
    ProjectPartition,
    WorktreePartition,
)
from hydra_codex.dashboard_event_refresh import current_worktree_roots
from hydra_codex.dashboard_refresh_state import REFRESH_STAGES
from hydra_codex.project import ProjectResolution
from hydra_codex.prepared_codex_events import (
    PreparedEventAttribution,
    attribute_prepared_codex_event_source,
)
from hydra_codex.rollout_identity import Pseudonymizer
from hydra_codex.rollout_sources import SourceChanged
from hydra_codex.storage import HydraStore
from tests.test_dashboard_refresh import worktree_partition


KEY = b"r" * 32


class DashboardEventRefreshTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = self.root / "hydra.sqlite3"
        self.query = SimpleNamespace(
            _refresh_snapshots_from_store=lambda *_args, **_kwargs: {},
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def worktree(
        self, project_id: str, name: str, worktree_path: str,
    ) -> WorktreePartition:
        root = self.root / name
        root.mkdir()
        resolution = ProjectResolution(
            project_id, root.resolve(), Path(worktree_path), name,
        )
        return WorktreePartition(root.resolve(), resolution, ())

    def event(self, raw_thread: str, name: str) -> CodexEventSource:
        return self.events((raw_thread,), name)

    def events(self, raw_threads: tuple[str, ...], name: str) -> CodexEventSource:
        path = self.root / name
        path.write_text("".join(
            json.dumps({
                "method": "thread/started",
                "params": {
                    "thread": {"id": raw_thread, "createdAt": 1720000000},
                },
            }) + "\n"
            for raw_thread in raw_threads
        ), encoding="utf-8")
        return CodexEventSource(path, APP_SERVER_V2)

    def rollout_worktree(
        self, project_id: str, name: str, *source_names: str,
    ) -> WorktreePartition:
        worktree = worktree_partition(project_id, self.root / name, *source_names)
        canonical = worktree.project_root.resolve()
        return replace(
            worktree,
            project_root=canonical,
            resolution=replace(worktree.resolution, project_root=canonical),
        )

    def bind(self, raw_thread: str, project_id: str, worktree_path: str) -> str:
        session_key = Pseudonymizer(KEY).digest("identity", raw_thread)
        store = HydraStore(self.database)
        try:
            with store.rollout_transaction() as connection:
                connection.execute(
                    """INSERT INTO sessions(
                           session_id,project_id,worktree_path,started_at,provenance)
                       VALUES (?,?,?,'2026-07-22T00:00:00Z','exact')""",
                    (session_key, project_id, worktree_path),
                )
                connection.execute(
                    """INSERT INTO trusted_turn_bindings(
                           turn_key,project_id,session_key,created_at)
                       VALUES (?,?,?,'2026-07-22T00:00:01Z')""",
                    (f"turn-{raw_thread}", project_id, session_key),
                )
        finally:
            store.close()
        return session_key

    def runner(self, plan: GlobalRolloutPlan, sources, **changes):
        return GlobalRefreshRunner(
            lambda: HydraStore(self.database),
            KEY,
            self.query,
            planner=lambda *_args, **_kwargs: plan,
            ingester=lambda *_args, **_kwargs: None,
            event_sources=sources,
            **changes,
        )

    def counts(self) -> tuple[int, int]:
        store = HydraStore(self.database)
        try:
            return (
                store.count("codex_event_sources"),
                store.count("codex_events"),
            )
        finally:
            store.close()

    def test_exact_binding_ingests_one_physical_stream_and_counts_event_progress(self) -> None:
        project_id = "project-a"
        worktree = self.worktree(project_id, "worktree-a", "feature/a")
        source = self.event("private-thread-a", "events-a.jsonl")
        self.bind("private-thread-a", project_id, "feature/a")
        plan = GlobalRolloutPlan(
            (ProjectPartition(project_id, (worktree, worktree)),), 0, 0, 0, (),
        )
        reconciles: list[str] = []
        progress = []
        opened: list[Path] = []
        original_open = os.open

        def observe_open(path, *args, **kwargs):
            if Path(path) == Path(source.path):
                opened.append(Path(path))
            return original_open(path, *args, **kwargs)

        runner = self.runner(
            plan,
            (source,),
            reconciler=lambda _store, project, _key: reconciles.append(project),
        )
        with patch("hydra_codex.rollout_sources.os.open", side_effect=observe_open):
            result = runner.run(lambda stage, value: progress.append((stage, value)))

        self.assertTrue(result.replace_all)
        self.assertEqual(result.diagnostic_codes, ())
        self.assertEqual(self.counts(), (1, 1))
        self.assertEqual(opened, [Path(source.path)])
        self.assertEqual(reconciles, [project_id])
        self.assertEqual(
            progress[-1][1].values(),
            (1, 1, 1, 1, 1, 1),
        )
        self.assertEqual(
            [REFRESH_STAGES.index(stage) for stage, _value in progress],
            sorted(REFRESH_STAGES.index(stage) for stage, _value in progress),
        )
        self.assertEqual(
            [stage for stage, _value in progress[:2]],
            ["inspect", "scan"],
        )

    def test_exact_sources_across_two_worktrees_share_one_project_reconcile(self) -> None:
        project_id = "project-a"
        first = self.worktree(project_id, "worktree-a", "feature/a")
        second = self.worktree(project_id, "worktree-b", "feature/b")
        source_a = self.event("private-thread-a", "events-a.jsonl")
        source_b = self.event("private-thread-b", "events-b.jsonl")
        self.bind("private-thread-a", project_id, "feature/a")
        self.bind("private-thread-b", project_id, "feature/b")
        plan = GlobalRolloutPlan(
            (ProjectPartition(project_id, (second, first)),), 0, 0, 0, (),
        )
        event_calls: list[tuple[str, Path, int]] = []
        reconciles: list[str] = []

        def ingest_events(
            _store, legacy_sources, project_root, owning_project, *,
            hash_key, prepared_sources,
        ):
            self.assertEqual(tuple(legacy_sources), ())
            self.assertEqual(hash_key, KEY)
            event_calls.append((
                owning_project, Path(project_root), len(tuple(prepared_sources)),
            ))

        result = self.runner(
            plan,
            (source_b, source_a),
            event_ingester=ingest_events,
            reconciler=lambda _store, project, _key: reconciles.append(project),
        ).run(lambda *_args: None)

        self.assertTrue(result.replace_all)
        self.assertEqual(event_calls, [
            (project_id, first.project_root, 1),
            (project_id, second.project_root, 1),
        ])
        self.assertEqual(reconciles, [project_id])

    def test_same_root_sources_are_persisted_in_one_event_call(self) -> None:
        project_id = "project-a"
        worktree = self.worktree(project_id, "worktree-a", "feature/a")
        source_a = self.event("private-thread-a", "events-a.jsonl")
        source_b = self.event("private-thread-b", "events-b.jsonl")
        self.bind("private-thread-a", project_id, "feature/a")
        self.bind("private-thread-b", project_id, "feature/a")
        plan = GlobalRolloutPlan(
            (ProjectPartition(project_id, (worktree,)),), 0, 0, 0, (),
        )
        calls: list[tuple[Path, int]] = []

        def event_ingester(
            _store, _legacy, project_root, _project, *,
            hash_key, prepared_sources,
        ):
            self.assertEqual(hash_key, KEY)
            calls.append((Path(project_root), len(tuple(prepared_sources))))

        result = self.runner(
            plan,
            (source_b, source_a),
            event_ingester=event_ingester,
        ).run(lambda *_args: None)

        self.assertTrue(result.replace_all)
        self.assertEqual(calls, [(worktree.project_root, 2)])

    def test_nested_rollout_resolutions_each_contribute_a_worktree_binding(self) -> None:
        project_id = "project-a"
        worktree = self.rollout_worktree(
            project_id, "worktree-a", "a.jsonl", "b.jsonl",
        )
        nested_paths = (Path("src/a"), Path("src/b"))
        nested_sources = tuple(
            replace(
                item,
                resolution=replace(
                    item.resolution,
                    project_root=worktree.project_root,
                    worktree_path=nested_path,
                ),
            )
            for item, nested_path in zip(
                worktree.sources, nested_paths, strict=True,
            )
        )
        worktree = replace(
            worktree,
            resolution=replace(
                worktree.resolution, worktree_path=Path("aggregate-only"),
            ),
            sources=nested_sources,
        )
        source_a = self.event("private-a", "events-a.jsonl")
        source_b = self.event("private-b", "events-b.jsonl")
        self.bind("private-a", project_id, "src/a")
        self.bind("private-b", project_id, "src/b")
        partition = ProjectPartition(project_id, (worktree,))
        plan = GlobalRolloutPlan((partition,), 2, 2, 2, ())
        event_calls: list[int] = []

        roots = current_worktree_roots((partition,))
        self.assertEqual(set(roots), {
            (project_id, "src/a"),
            (project_id, "src/b"),
        })
        result = self.runner(
            plan,
            (source_b, source_a),
            event_ingester=lambda *_args, prepared_sources, **_kwargs: (
                event_calls.append(len(tuple(prepared_sources)))
            ),
        ).run(lambda *_args: None)

        self.assertTrue(result.replace_all)
        self.assertEqual(event_calls, [2])

    def test_exact_binding_without_current_plan_root_is_unavailable_without_writes(self) -> None:
        source = self.event("private-thread-a", "events-a.jsonl")
        self.bind("private-thread-a", "project-a", "feature/missing")
        plan = GlobalRolloutPlan((), 0, 0, 0, ())

        result = self.runner(plan, (source,)).run(lambda *_args: None)

        self.assertFalse(result.replace_all)
        self.assertEqual(
            result.diagnostic_codes, ("event_attribution_unavailable",),
        )
        self.assertEqual(self.counts(), (0, 0))

    def test_invalid_or_unparseable_sources_are_unavailable_without_scan_credit(self) -> None:
        project_id = "project-a"
        worktree = self.worktree(project_id, "worktree-a", "feature/a")
        source = self.event("private-thread", "events.jsonl")
        self.bind("private-thread", project_id, "feature/a")
        plan = GlobalRolloutPlan(
            (ProjectPartition(project_id, (worktree,)),), 0, 0, 0, (),
        )
        progress = []

        result = self.runner(
            plan,
            (source, object()),
            event_preparer=Mock(
                side_effect=EventAdapterError("private malformed payload"),
            ),
        ).run(lambda stage, value: progress.append((stage, value)))

        self.assertFalse(result.replace_all)
        self.assertEqual(
            result.diagnostic_codes, ("event_attribution_unavailable",),
        )
        self.assertEqual(progress[-1][1].values(), (2, 2, 0, 1, 1, 1))
        self.assertEqual(self.counts(), (0, 0))

        changed_progress = []
        changed = self.runner(
            plan,
            (source,),
            event_preparer=Mock(side_effect=SourceChanged("private source")),
        ).run(lambda stage, value: changed_progress.append((stage, value)))
        self.assertEqual(changed.diagnostic_codes, ("source_changed",))
        self.assertEqual(changed_progress[-1][1].values(), (1, 1, 0, 1, 1, 1))

    def test_empty_unbound_and_partially_bound_sources_are_unavailable(self) -> None:
        project_id = "project-a"
        worktree = self.worktree(project_id, "worktree-a", "feature/a")
        empty = self.events((), "empty.jsonl")
        unbound = self.event("private-unbound", "unbound.jsonl")
        partial = self.events(
            ("private-bound", "private-missing"), "partial.jsonl",
        )
        self.bind("private-bound", project_id, "feature/a")
        plan = GlobalRolloutPlan(
            (ProjectPartition(project_id, (worktree,)),), 0, 0, 0, (),
        )
        observed = []

        result = self.runner(plan, (empty, unbound, partial)).run(
            lambda stage, value: observed.append((stage, value)),
        )

        self.assertFalse(result.replace_all)
        self.assertEqual(
            result.diagnostic_codes, ("event_attribution_unavailable",),
        )
        self.assertEqual(self.counts(), (0, 0))
        self.assertEqual(observed[-1][1].values(), (3, 3, 3, 1, 1, 1))

    def test_mixed_bindings_and_duplicate_current_roots_are_ambiguous(self) -> None:
        first = self.worktree("project-a", "worktree-a", "feature/a")
        duplicate_one = self.worktree("project-a", "duplicate-one", "shared")
        duplicate_two = self.worktree("project-a", "duplicate-two", "shared")
        second = self.worktree("project-b", "worktree-b", "feature/b")
        mixed = self.events(("mixed-a", "mixed-b"), "mixed.jsonl")
        duplicate = self.event("duplicate", "duplicate.jsonl")
        self.bind("mixed-a", "project-a", "feature/a")
        self.bind("mixed-b", "project-b", "feature/b")
        self.bind("duplicate", "project-a", "shared")
        plan = GlobalRolloutPlan((
            ProjectPartition(
                "project-a", (first, duplicate_one, duplicate_two),
            ),
            ProjectPartition("project-b", (second,)),
        ), 0, 0, 0, ())

        result = self.runner(plan, (mixed, duplicate)).run(lambda *_args: None)

        self.assertFalse(result.replace_all)
        self.assertEqual(
            result.diagnostic_codes, ("event_attribution_ambiguous",),
        )
        self.assertEqual(self.counts(), (0, 0))

    def test_root_replacement_after_attribution_is_source_changed(self) -> None:
        project_id = "project-a"
        worktree = self.worktree(project_id, "worktree-a", "feature/a")
        source = self.event("private-thread", "events.jsonl")
        self.bind("private-thread", project_id, "feature/a")
        plan = GlobalRolloutPlan(
            (ProjectPartition(project_id, (worktree,)),), 0, 0, 0, (),
        )
        moved = self.root / "moved-worktree"

        def replace_after_attribution(connection, prepared, roots):
            result = attribute_prepared_codex_event_source(
                connection, prepared, roots,
            )
            self.assertIsInstance(result, PreparedEventAttribution)
            worktree.project_root.rename(moved)
            worktree.project_root.mkdir()
            return result

        reconciler = Mock()
        result = self.runner(
            plan,
            (source,),
            event_attributor=replace_after_attribution,
            reconciler=reconciler,
        ).run(lambda *_args: None)

        self.assertFalse(result.replace_all)
        self.assertEqual(result.diagnostic_codes, ("source_changed",))
        self.assertEqual(self.counts(), (0, 0))
        reconciler.assert_not_called()

    def test_root_swap_by_event_ingester_is_revalidated_and_rolled_back(self) -> None:
        project_id = "project-a"
        worktree = self.worktree(project_id, "worktree-a", "feature/a")
        source = self.event("private-thread", "events.jsonl")
        self.bind("private-thread", project_id, "feature/a")
        plan = GlobalRolloutPlan(
            (ProjectPartition(project_id, (worktree,)),), 0, 0, 0, (),
        )
        moved = self.root / "moved-after-ingest"

        def swap_after_ingest(*args, **kwargs):
            ingest_codex_events(*args, **kwargs)
            worktree.project_root.rename(moved)
            worktree.project_root.mkdir()

        reconciler = Mock()
        result = self.runner(
            plan,
            (source,),
            event_ingester=swap_after_ingest,
            reconciler=reconciler,
        ).run(lambda *_args: None)

        self.assertFalse(result.replace_all)
        self.assertEqual(result.diagnostic_codes, ("source_changed",))
        self.assertEqual(self.counts(), (0, 0))
        reconciler.assert_not_called()

    def test_event_failure_rolls_back_rollout_and_event_writes(self) -> None:
        project_id = "project-a"
        worktree = self.rollout_worktree(
            project_id, "worktree-a", "rollout.jsonl",
        )
        source = self.event("private-thread", "events.jsonl")
        self.bind("private-thread", project_id, ".")
        plan = GlobalRolloutPlan(
            (ProjectPartition(project_id, (worktree,)),), 1, 1, 1, (),
        )

        def ingest_rollout(store, _roots, _root, owning_project, **_kwargs):
            store.connection.execute(
                """INSERT INTO dashboard_projects(
                       project_id,display_name,first_seen_at,last_seen_at)
                   VALUES (?,?,'2026-07-22T00:00:00Z','2026-07-22T00:00:00Z')""",
                (owning_project, "private display"),
            )

        def fail_after_event_write(*args, **kwargs):
            ingest_codex_events(*args, **kwargs)
            raise RuntimeError("private event failure")

        reconciler = Mock()
        result = GlobalRefreshRunner(
            lambda: HydraStore(self.database), KEY, self.query,
            planner=lambda *_args, **_kwargs: plan,
            ingester=ingest_rollout,
            event_sources=(source,),
            event_ingester=fail_after_event_write,
            reconciler=reconciler,
        ).run(lambda *_args: None)

        store = HydraStore(self.database)
        try:
            project_count = store.connection.execute(
                "SELECT COUNT(*) FROM dashboard_projects",
            ).fetchone()[0]
        finally:
            store.close()
        self.assertEqual(project_count, 0)
        self.assertEqual(self.counts(), (0, 0))
        self.assertEqual(result.diagnostic_codes, ("internal_failure",))
        self.assertFalse(result.replace_all)
        reconciler.assert_not_called()

    def test_event_failure_isolated_to_project_while_other_project_commits(self) -> None:
        first = self.rollout_worktree(
            "project-a", "worktree-a", "rollout-a.jsonl",
        )
        second = self.rollout_worktree(
            "project-b", "worktree-b", "rollout-b.jsonl",
        )
        source_a = self.event("private-a", "events-a.jsonl")
        source_b = self.event("private-b", "events-b.jsonl")
        self.bind("private-a", "project-a", ".")
        self.bind("private-b", "project-b", ".")
        plan = GlobalRolloutPlan((
            ProjectPartition("project-b", (second,)),
            ProjectPartition("project-a", (first,)),
        ), 2, 2, 2, ())
        transaction_states: list[tuple[str, bool, str]] = []

        def ingest_rollout(store, _roots, _root, project_id, **_kwargs):
            store.connection.execute(
                """INSERT INTO dashboard_projects(
                       project_id,display_name,first_seen_at,last_seen_at)
                   VALUES (?,?,'2026-07-22T00:00:00Z','2026-07-22T00:00:00Z')""",
                (project_id, project_id),
            )

        def selectively_fail(*args, **kwargs):
            transaction_states.append((
                args[3], args[0].connection.in_transaction, "before",
            ))
            ingest_codex_events(*args, **kwargs)
            transaction_states.append((
                args[3], args[0].connection.in_transaction, "after",
            ))
            if args[3] == "project-a":
                raise RuntimeError("private project-a event failure")

        reconciles: list[str] = []
        runner = GlobalRefreshRunner(
            lambda: HydraStore(self.database), KEY, self.query,
            planner=lambda *_args, **_kwargs: plan,
            ingester=ingest_rollout,
            event_sources=(source_b, source_a),
            event_ingester=selectively_fail,
            reconciler=lambda _store, project, _key: reconciles.append(project),
        )

        result = runner.run(lambda *_args: None)

        store = HydraStore(self.database)
        try:
            projects = [row[0] for row in store.connection.execute(
                "SELECT project_id FROM dashboard_projects ORDER BY project_id",
            )]
            event_projects = [row[0] for row in store.connection.execute(
                "SELECT project_id FROM codex_event_sources ORDER BY project_id",
            )]
        finally:
            store.close()
        self.assertEqual(transaction_states, [
            ("project-a", True, "before"),
            ("project-a", True, "after"),
            ("project-b", True, "before"),
            ("project-b", True, "after"),
        ])
        self.assertEqual(projects, ["project-b"])
        self.assertEqual(event_projects, ["project-b"])
        self.assertEqual(reconciles, ["project-b"])
        self.assertEqual(result.diagnostic_codes, ("internal_failure",))
        self.assertEqual(
            (result.projects_total, result.projects_completed, result.projects_refreshed),
            (2, 2, 1),
        )

    def test_repeat_is_idempotent_and_zero_sources_do_not_prepare_or_spawn(self) -> None:
        project_id = "project-a"
        worktree = self.worktree(project_id, "worktree-a", "feature/a")
        source = self.event("private-thread", "events.jsonl")
        self.bind("private-thread", project_id, "feature/a")
        plan = GlobalRolloutPlan(
            (ProjectPartition(project_id, (worktree,)),), 0, 0, 0, (),
        )
        runner = self.runner(plan, (source,))

        with patch("subprocess.run") as run, patch("subprocess.Popen") as popen:
            first = runner.run(lambda *_args: None)
            second = runner.run(lambda *_args: None)

        self.assertTrue(first.replace_all)
        self.assertTrue(second.replace_all)
        self.assertEqual(self.counts(), (1, 1))
        run.assert_not_called()
        popen.assert_not_called()

        preparer = Mock(side_effect=AssertionError("empty source preparation"))
        empty = self.runner(plan, (), event_preparer=preparer).run(
            lambda *_args: None,
        )
        self.assertTrue(empty.replace_all)
        preparer.assert_not_called()

    def test_interleaved_rollout_scan_callbacks_never_regress_public_stage(self) -> None:
        project_id = "project-a"
        worktree = self.worktree(project_id, "worktree-a", "feature/a")
        source = self.event("private-thread", "events.jsonl")
        self.bind("private-thread", project_id, "feature/a")
        plan = GlobalRolloutPlan(
            (ProjectPartition(project_id, (worktree,)),), 2, 2, 2, (),
        )

        def interleaved_planner(_store, _roots, _key, progress):
            progress("discover", 2, 2)
            progress("inspect", 1, 2)
            progress("scan", 1, 2)
            progress("inspect", 2, 2)
            progress("scan", 2, 2)
            return plan

        stages = []
        runner = GlobalRefreshRunner(
            lambda: HydraStore(self.database), KEY, self.query,
            planner=interleaved_planner,
            event_sources=(source,),
            ingester=lambda *_args, **_kwargs: None,
            event_ingester=lambda *_args, **_kwargs: None,
            reconciler=lambda *_args, **_kwargs: None,
        )

        result = runner.run(lambda stage, value: stages.append((stage, value)))

        self.assertTrue(result.replace_all)
        indexes = [REFRESH_STAGES.index(stage) for stage, _value in stages]
        self.assertEqual(indexes, sorted(indexes))
        self.assertEqual(stages[-1][1].values()[:3], (3, 3, 3))
        first_scan = next(
            index for index, (stage, _value) in enumerate(stages)
            if stage == "scan"
        )
        self.assertNotIn("inspect", [stage for stage, _value in stages[first_scan:]])


if __name__ == "__main__":
    unittest.main()
