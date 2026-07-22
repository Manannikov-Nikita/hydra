from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from hydra_codex.dashboard_refresh import (
    AttributedRollout,
    CachedRollout,
    DashboardSnapshotCache,
    GlobalRolloutPlan,
    GlobalRefreshRunner,
    ProjectPartition,
    RefreshController,
    RefreshProgress,
    RefreshResult,
    RefreshSnapshot,
    plan_global_rollout_ingest,
    trusted_rollout_roots,
    WorktreePartition,
)
from hydra_codex.project import ProjectResolution
from hydra_codex.rollout_identity import RolloutRoot, TrustedRolloutCandidate
from hydra_codex.rollout_sources import SourceChanged, SourceScan, SourceStat
from hydra_codex.storage import HydraStore, StorageUnavailable
from tests.test_dashboard_model import DashboardModelTests


KEY = b"r" * 32
REF_A = "project_0123456789ab"
REF_B = "project_abcdef012345"
REF_NEW = "project_deadbeef0123"


def snapshot(project_ref: str):
    base = DashboardModelTests().snapshot()
    summary = replace(base.projects[0], project_ref=project_ref)
    return replace(
        base,
        projects=(summary,),
        selected_project_ref=None,
        project_json=None,
        selected_task_json=None,
    )


def progress(**changes: int) -> RefreshProgress:
    return replace(RefreshProgress(), **changes)


def worktree_partition(
    project_id: str, root: Path, *source_names: str,
) -> WorktreePartition:
    root.mkdir(parents=True, exist_ok=True)
    resolution = ProjectResolution(project_id, root, Path("."), project_id.title())
    attributed = []
    for index, name in enumerate(source_names, start=1):
        path = root / name
        path.write_text("{}\n", encoding="utf-8")
        details = path.stat()
        source_stat = SourceStat(
            details.st_dev, details.st_ino, details.st_size,
            details.st_mtime_ns, details.st_ctime_ns,
        )
        scan = SourceScan(
            str(index) * 64, (str(index) * 64,), 1, details.st_size,
            str(index) * 64, "session", "conversation", str(root),
            "2026-07-22T10:00:00Z", str(index), source_stat, path,
        )
        candidate = TrustedRolloutCandidate(path, "active", path, True)
        attributed.append(AttributedRollout(candidate, scan, resolution))
    return WorktreePartition(root, resolution, tuple(attributed))


class BlockingRunner:
    def __init__(self, result: RefreshResult) -> None:
        self.result = result
        self.calls = 0
        self.entered = threading.Event()
        self.release = threading.Event()
        self.thread_ids: list[int] = []

    def run(self, report):
        self.calls += 1
        self.thread_ids.append(threading.get_ident())
        report("discover", progress(sources_discovered=1))
        self.entered.set()
        self.release.wait(2)
        return self.result


class RefreshControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.before_a = snapshot(REF_A)
        self.before_b = snapshot(REF_B)
        self.cache = DashboardSnapshotCache({REF_A: self.before_a, REF_B: self.before_b})
        self.clock_values = iter((
            datetime(2026, 7, 22, 10, 0, tzinfo=timezone.utc),
            datetime(2026, 7, 22, 10, 1, tzinfo=timezone.utc),
            datetime(2026, 7, 22, 10, 2, tzinfo=timezone.utc),
            datetime(2026, 7, 22, 10, 3, tzinfo=timezone.utc),
        ))

    def controller(self, runner: BlockingRunner) -> RefreshController:
        refs = iter(("refresh_0123456789ab", "refresh_abcdef012345"))
        return RefreshController(
            self.cache, runner, clock=lambda: next(self.clock_values),
            ref_factory=lambda: next(refs),
        )

    def test_start_is_single_flight_and_terminal_allows_a_new_job(self) -> None:
        runner = BlockingRunner(RefreshResult({}, True, (), 0, 0, 0))
        controller = self.controller(runner)

        first, reused_first = controller.start()
        self.assertTrue(runner.entered.wait(1))
        second, reused_second = controller.start()

        self.assertFalse(reused_first)
        self.assertTrue(reused_second)
        self.assertEqual(second.refresh_ref, first.refresh_ref)
        self.assertEqual(runner.calls, 1)
        runner.release.set()
        controller.close()
        third, reused_third = controller.start()
        self.assertFalse(reused_third)
        self.assertNotEqual(third.refresh_ref, first.refresh_ref)
        controller.close()

    def test_running_overlay_does_not_mutate_cached_base(self) -> None:
        runner = BlockingRunner(RefreshResult({}, True, (), 0, 0, 0))
        controller = self.controller(runner)
        controller.start()
        self.assertTrue(runner.entered.wait(1))

        served = controller.snapshot(REF_A)

        self.assertIs(self.cache.get(REF_A), self.before_a)
        self.assertIsNot(served, self.before_a)
        self.assertEqual(served.refresh.state, "running")
        self.assertEqual(served.refresh.stage, "discover")
        runner.release.set()
        controller.close()

    def test_partial_publish_preserves_failed_identity_and_hides_new_projects(self) -> None:
        refreshed_a = snapshot(REF_A)
        runner = BlockingRunner(RefreshResult(
            {REF_A: refreshed_a, REF_NEW: snapshot(REF_NEW)},
            False,
            ("source_changed",),
            2, 2, 1,
        ))
        controller = self.controller(runner)
        controller.start()
        self.assertTrue(runner.entered.wait(1))
        runner.release.set()
        controller.close()

        self.assertIsNot(self.cache.get(REF_A), self.before_a)
        self.assertEqual(self.cache.get(REF_A).refresh.state, "partial")
        self.assertIs(self.cache.get(REF_B), self.before_b)
        self.assertIsNone(self.cache.get(REF_NEW))
        self.assertEqual(controller.current().state, "partial")

    def test_success_replaces_all_refs_and_failed_refresh_retains_all(self) -> None:
        replacement = snapshot(REF_NEW)
        successful = BlockingRunner(RefreshResult(
            {REF_NEW: replacement}, True, (), 1, 1, 1,
        ))
        controller = self.controller(successful)
        controller.start()
        self.assertTrue(successful.entered.wait(1))
        successful.release.set()
        controller.close()
        self.assertEqual(self.cache.refs(), (REF_NEW,))
        self.assertEqual(self.cache.get(REF_NEW).refresh.state, "succeeded")

        failed_before = self.cache.get(REF_NEW)
        failed_runner = BlockingRunner(RefreshResult(
            {}, False, ("internal_failure",), 1, 1, 0,
        ))
        failed_controller = RefreshController(
            self.cache, failed_runner,
            clock=lambda: datetime(2026, 7, 22, 11, 0, tzinfo=timezone.utc),
            ref_factory=lambda: "refresh_111111111111",
        )
        failed_controller.start()
        self.assertTrue(failed_runner.entered.wait(1))
        failed_runner.release.set()
        failed_controller.close()
        self.assertIs(self.cache.get(REF_NEW), failed_before)
        self.assertEqual(failed_controller.current().state, "failed")

    def test_progress_and_failure_payloads_are_categorical_and_private(self) -> None:
        runner = BlockingRunner(RefreshResult(
            {}, False, ("project_root_unavailable",), 1, 1, 0,
        ))
        controller = self.controller(runner)
        controller.start()
        self.assertTrue(runner.entered.wait(1))
        runner.release.set()
        controller.close()

        current = controller.current()
        self.assertEqual(current.stage, None)
        self.assertNotIn("/Users/private", repr(current))
        self.assertEqual(
            current.to_view().diagnostic_codes, ("project_root_unavailable",),
        )
        self.assertTrue(all(
            fact.provenance == "derived"
            for fact in current.to_view().progress.values()
        ))
        with self.assertRaises(ValueError):
            RefreshSnapshot(
                "refresh_0123456789ab", "running", "scan",
                "2026-07-22T10:00:00Z", None,
                progress(sources_scanned=-1), (),
            )
        with self.assertRaises(ValueError):
            RefreshSnapshot(
                "refresh_0123456789ab", "queued", None,
                "not-a-timestamp", None, RefreshProgress(), (),
            )

    def test_terminal_snapshot_returns_exact_published_object(self) -> None:
        replacement = snapshot(REF_A)
        runner = BlockingRunner(RefreshResult(
            {REF_A: replacement}, True, (), 1, 1, 1,
        ))
        controller = self.controller(runner)
        controller.start()
        self.assertTrue(runner.entered.wait(1))
        runner.release.set()
        controller.close()

        published = self.cache.get(REF_A)
        self.assertIs(controller.snapshot(REF_A), published)

    def test_empty_cache_and_invalid_results_fail_closed(self) -> None:
        self.assertEqual(DashboardSnapshotCache().refs(), ())
        with self.assertRaises(TypeError):
            RefreshResult({REF_A: object()}, False, (), 1, 1, 1)
        with self.assertRaises(ValueError):
            RefreshResult({}, True, ("source_changed",), 0, 0, 0)

    def test_regressing_progress_fails_the_job(self) -> None:
        class RegressingRunner:
            def run(self, report):
                report("discover", progress(sources_discovered=2))
                report(
                    "inspect",
                    progress(sources_discovered=1, sources_inspected=1),
                )
                raise AssertionError("unreachable")

        controller = self.controller(RegressingRunner())
        controller.start()
        controller.close()

        self.assertEqual(controller.current().state, "failed")
        self.assertEqual(controller.current().diagnostic_codes, ("internal_failure",))

    def test_close_is_bounded_and_only_waits_for_owned_worker(self) -> None:
        import time

        runner = BlockingRunner(RefreshResult({}, True, (), 0, 0, 0))
        controller = self.controller(runner)
        controller.start()
        self.assertTrue(runner.entered.wait(1))

        started = time.monotonic()
        controller.close(0.01)
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 0.2)
        self.assertEqual(controller.current().state, "running")
        runner.release.set()
        controller.close()


class GlobalPlannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = HydraStore(self.root / "hydra.sqlite3")

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def source_scan(self, path: Path, cwd: Path, marker: str) -> SourceScan:
        details = path.stat()
        stat = SourceStat(
            details.st_dev, details.st_ino, details.st_size,
            details.st_mtime_ns, details.st_ctime_ns,
        )
        return SourceScan(
            marker * 64, (marker * 64,), 1, details.st_size, marker * 64,
            "session", "conversation", str(cwd),
            "2026-07-22T10:00:00Z", marker, stat, path,
        )

    def test_trusted_roots_are_only_active_and_archive_under_explicit_home(self) -> None:
        (self.root / ".codex/sessions").mkdir(parents=True)
        (self.root / ".codex/archived_sessions").mkdir()

        roots = trusted_rollout_roots({"HOME": str(self.root)})

        self.assertEqual(
            tuple((Path(item.path), item.label) for item in roots),
            (
                (self.root / ".codex/sessions", "active"),
                (self.root / ".codex/archived_sessions", "archived"),
            ),
        )

    def test_plan_discovers_once_scans_changed_once_and_partitions_worktrees(self) -> None:
        project_a = self.root / "project-a"
        project_b = self.root / "project-b"
        for project, project_id in ((project_a, "a"), (project_b, "b")):
            (project / ".hydra").mkdir(parents=True)
            (project / ".hydra/project.toml").write_text(
                f'project_id = "project-{project_id}"\n', encoding="utf-8",
            )
        source_a = self.root / "a.jsonl"
        source_b = self.root / "b.jsonl"
        source_a.write_text("{}\n", encoding="utf-8")
        source_b.write_text("{}\n", encoding="utf-8")
        candidates = (
            TrustedRolloutCandidate(source_b, "archived", source_b, True),
            TrustedRolloutCandidate(source_a, "active", source_a, True),
        )
        scans = {
            source_a: self.source_scan(source_a, project_a, "a"),
            source_b: self.source_scan(source_b, project_b, "b"),
        }
        calls = {"discover": 0, "scan": []}

        def discover(_roots):
            calls["discover"] += 1
            return candidates

        def scan(path, _key, _pseudonymize):
            calls["scan"].append(path)
            return scans[path]

        plan = plan_global_rollout_ingest(
            self.store,
            (RolloutRoot(self.root, "active"),),
            KEY,
            discover=discover,
            scanner=scan,
            revalidate=lambda item: scans[item.path].source_stat,
        )

        self.assertEqual(calls, {"discover": 1, "scan": [source_a, source_b]})
        self.assertEqual(plan.project_count, 2)
        self.assertEqual(plan.source_count, 2)
        self.assertNotIn(str(self.root), repr(plan))
        self.assertNotIn("project-a", repr(plan))
        self.assertEqual(plan.as_dict()["diagnostic_codes"], [])

    def test_unchanged_location_avoids_scan_but_remains_an_affected_project(self) -> None:
        source = self.root / "cached.jsonl"
        source.write_text("{}\n", encoding="utf-8")
        scan = self.source_scan(source, self.root, "d")
        candidate = TrustedRolloutCandidate(source, "archived", source, True)
        scanner_calls: list[Path] = []

        with patch(
            "hydra_codex.dashboard_refresh_plan.unchanged_location_attribution",
            return_value=SimpleNamespace(
                project_id="project-cached", logical="logical", revision="revision",
            ),
        ):
            plan = plan_global_rollout_ingest(
                self.store, (RolloutRoot(source, "archived"),), KEY,
                discover=lambda _roots: (candidate,),
                scanner=lambda path, *_args: scanner_calls.append(path),
                revalidate=lambda _item: scan.source_stat,
            )

        self.assertEqual(scanner_calls, [])
        self.assertEqual(plan.project_count, 1)
        self.assertEqual(plan.partitions[0].cached_count, 1)
        self.assertEqual(plan.partitions[0].worktrees, ())
        self.assertNotIn("project-cached", repr(plan))

    def test_unavailable_and_ambiguous_sources_are_categorical_without_paths(self) -> None:
        missing = self.root / "missing.jsonl"
        source = self.root / "source.jsonl"
        source.write_text("{}\n", encoding="utf-8")
        bad_scan = self.source_scan(source, Path("relative"), "c")
        candidate = TrustedRolloutCandidate(source, "active", source, True)

        plan = plan_global_rollout_ingest(
            self.store, (RolloutRoot(self.root, "active"),), KEY,
            discover=lambda _roots: (candidate,),
            scanner=lambda *_args: bad_scan,
            revalidate=lambda _item: bad_scan.source_stat,
        )

        self.assertEqual(plan.project_count, 0)
        self.assertEqual(plan.diagnostic_codes, ("project_root_unavailable",))
        self.assertNotIn(str(missing), repr(plan))


class GlobalRunnerTests(unittest.TestCase):
    def test_defaults_to_no_event_sources_and_worker_owns_store(self) -> None:
        created_on: list[int] = []
        closed_on: list[int] = []

        temporary = tempfile.TemporaryDirectory()

        class Store(HydraStore):
            def __init__(self):
                super().__init__(Path(temporary.name) / "hydra.sqlite3")

            def close(self):
                closed_on.append(threading.get_ident())
                super().close()

        def factory():
            created_on.append(threading.get_ident())
            return Store()

        query = SimpleNamespace(
            _refresh_snapshots_from_store=lambda *_args, **_kwargs: {},
        )
        runner = GlobalRefreshRunner(
            factory,
            KEY,
            query_service=query,
            roots=(),
            planner=lambda *_args, **_kwargs: type("Plan", (), {
                "partitions": (), "diagnostic_codes": (),
                "source_count": 0, "scanned_count": 0,
            })(),
        )
        result: list[RefreshResult] = []
        worker = threading.Thread(target=lambda: result.append(runner.run(lambda *_: None)))
        worker.start()
        worker.join(1)

        self.assertFalse(worker.is_alive())
        self.assertEqual(created_on, closed_on)
        self.assertNotEqual(created_on, [threading.get_ident()])
        self.assertTrue(result[0].replace_all)
        temporary.cleanup()

    def test_only_sqlite_busy_or_locked_is_reported_as_database_busy(self) -> None:
        busy = sqlite3.OperationalError("private database path is busy")
        busy.sqlite_errorcode = sqlite3.SQLITE_BUSY
        other = sqlite3.OperationalError("private database path is invalid")

        self.assertEqual(GlobalRefreshRunner._code(busy), "database_busy")
        self.assertEqual(GlobalRefreshRunner._code(other), "internal_failure")

    def test_planner_failures_are_categorical_results(self) -> None:
        query = SimpleNamespace(
            _refresh_snapshots_from_store=lambda *_args, **_kwargs: {},
        )
        for error, expected in (
            (SourceChanged("private rollout path"), "source_changed"),
            (StorageUnavailable("private database path"), "storage_unavailable"),
        ):
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as directory:
                runner = GlobalRefreshRunner(
                    lambda: HydraStore(Path(directory) / "hydra.sqlite3"),
                    KEY,
                    query,
                    planner=lambda *_args, error=error, **_kwargs: (_ for _ in ()).throw(error),
                )

                result = runner.run(lambda *_args: None)

                self.assertEqual(result.diagnostic_codes, (expected,))
                self.assertFalse(result.replace_all)

    def test_cached_project_reconciles_without_prepared_ingest(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        database = Path(temporary.name) / "hydra.sqlite3"
        cached = CachedRollout(
            "project-cached", "logical", "revision", "location", "archived",
        )
        plan = GlobalRolloutPlan(
            (ProjectPartition("project-cached", (), (cached,)),),
            1, 1, 0, (),
        )
        ingests: list[object] = []
        reconciles: list[str] = []
        refreshed: list[str] = []
        query = SimpleNamespace(
            _refresh_snapshots_from_store=lambda *_args, **_kwargs: {},
        )
        runner = GlobalRefreshRunner(
            lambda: HydraStore(database), KEY, query,
            planner=lambda *_args, **_kwargs: plan,
            ingester=lambda *_args, **_kwargs: ingests.append(object()),
            reconciler=lambda _store, project_id, _key: reconciles.append(project_id),
            cached_refresher=lambda _store, item: refreshed.append(item.label),
        )

        result = runner.run(lambda *_args: None)

        self.assertEqual(ingests, [])
        self.assertEqual(reconciles, ["project-cached"])
        self.assertEqual(refreshed, ["archived"])
        self.assertTrue(result.replace_all)
        temporary.cleanup()

    def test_multi_worktree_failure_rolls_back_project_and_projects_are_independent(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        database = root / "hydra.sqlite3"
        worktree_a2 = worktree_partition("project-a", root / "z-worktree", "a2.jsonl")
        worktree_a1 = worktree_partition("project-a", root / "a-worktree", "a1.jsonl")
        worktree_b = worktree_partition("project-b", root / "b-worktree", "b.jsonl")
        plan = GlobalRolloutPlan((
            ProjectPartition("project-b", (worktree_b,)),
            ProjectPartition("project-a", (worktree_a2, worktree_a1)),
        ), 3, 3, 3, ())
        calls: list[tuple[str, str]] = []
        reconciles: list[str] = []

        def ingest(store, _roots, project_root, project_id, **_kwargs):
            calls.append((project_id, Path(project_root).name))
            with store.rollout_transaction() as connection:
                connection.execute(
                    """INSERT INTO dashboard_projects(
                           project_id,display_name,first_seen_at,last_seen_at)
                       VALUES (?,?,?,?) ON CONFLICT(project_id) DO UPDATE SET
                         display_name=excluded.display_name""",
                    (
                        project_id, Path(project_root).name,
                        "2026-07-22T10:00:00Z", "2026-07-22T10:00:00Z",
                    ),
                )
            if Path(project_root).name == "z-worktree":
                raise SourceChanged("private source failed")

        query = SimpleNamespace(
            _refresh_snapshots_from_store=lambda *_args, **_kwargs: {},
        )
        runner = GlobalRefreshRunner(
            lambda: HydraStore(database), KEY, query,
            planner=lambda *_args, **_kwargs: plan,
            ingester=ingest,
            reconciler=lambda _store, project_id, _key: reconciles.append(project_id),
        )

        result = runner.run(lambda *_args: None)

        self.assertEqual(calls, [
            ("project-a", "a-worktree"),
            ("project-a", "z-worktree"),
            ("project-b", "b-worktree"),
        ])
        self.assertEqual(reconciles, ["project-b"])
        store = HydraStore(database)
        try:
            rows = [tuple(row) for row in store.connection.execute(
                "SELECT project_id,display_name FROM dashboard_projects ORDER BY project_id",
            )]
        finally:
            store.close()
        self.assertEqual(rows, [("project-b", "b-worktree")])
        self.assertEqual(result.diagnostic_codes, ("source_changed",))
        self.assertFalse(result.replace_all)
        temporary.cleanup()

    def test_catalog_and_dto_failure_roll_back_the_full_publication_barrier(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        database = root / "hydra.sqlite3"
        worktree = worktree_partition("project-new", root / "worktree", "new.jsonl")
        plan = GlobalRolloutPlan(
            (ProjectPartition("project-new", (worktree,)),), 1, 1, 1, (),
        )
        query = SimpleNamespace(
            _refresh_snapshots_from_store=lambda *_args, **_kwargs: (
                _ for _ in ()
            ).throw(ValueError("private DTO detail")),
        )
        runner = GlobalRefreshRunner(
            lambda: HydraStore(database), KEY, query,
            planner=lambda *_args, **_kwargs: plan,
            ingester=lambda *_args, **_kwargs: None,
            reconciler=lambda *_args, **_kwargs: None,
        )

        result = runner.run(lambda *_args: None)

        store = HydraStore(database)
        try:
            count = store.connection.execute(
                "SELECT COUNT(*) FROM dashboard_projects",
            ).fetchone()[0]
        finally:
            store.close()
        self.assertEqual(count, 0)
        self.assertEqual(result.snapshots, {})
        self.assertEqual(result.diagnostic_codes, ("internal_failure",))
        temporary.cleanup()

    def test_configured_event_sources_fail_closed_without_reads_or_subprocess(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        database = Path(temporary.name) / "hydra.sqlite3"

        class UnreadableEvent:
            def __fspath__(self):
                raise AssertionError("event source must not be opened")

        plan = GlobalRolloutPlan((), 0, 0, 0, ())
        query = SimpleNamespace(
            _refresh_snapshots_from_store=lambda *_args, **_kwargs: {},
        )
        runner = GlobalRefreshRunner(
            lambda: HydraStore(database), KEY, query,
            planner=lambda *_args, **_kwargs: plan,
            event_sources=(UnreadableEvent(),),
        )

        with patch("subprocess.run") as run, patch("subprocess.Popen") as popen:
            result = runner.run(lambda *_args: None)

        run.assert_not_called()
        popen.assert_not_called()
        self.assertEqual(
            result.diagnostic_codes, ("event_attribution_unavailable",),
        )
        self.assertFalse(result.replace_all)
        self.assertNotIn(str(database), repr(runner))
        temporary.cleanup()


if __name__ == "__main__":
    unittest.main()
