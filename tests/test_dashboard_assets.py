from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import subprocess
import unittest


ASSET_ROOT = Path(__file__).parents[1] / "src" / "hydra_codex" / "dashboard_assets"
EXPECTED_ASSETS = {
    "index.html",
    "tokens.css",
    "dashboard.css",
    "bootstrap.js",
    "api.js",
    "state.js",
    "dom.js",
    "app.js",
    "views/shell.js",
    "views/overview.js",
    "views/tasks.js",
    "views/compare.js",
    "views/health.js",
    "views/evidence.js",
}


class DashboardAssetContractTests(unittest.TestCase):
    def asset(self, name: str) -> str:
        return (ASSET_ROOT / name).read_text(encoding="utf-8")

    def all_sources(self) -> str:
        return "\n".join(self.asset(name) for name in sorted(EXPECTED_ASSETS))

    def evaluate_dom(self, expression: str) -> object:
        source = (
            f"import * as dom from {json.dumps((ASSET_ROOT / 'dom.js').as_uri())};"
            f"process.stdout.write(JSON.stringify({expression}));"
        )
        completed = subprocess.run(
            ["node", "--input-type=module", "-e", source],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout)

    def test_inventory_is_exact_utf8_and_self_contained(self) -> None:
        inventory = {
            path.relative_to(ASSET_ROOT).as_posix()
            for path in ASSET_ROOT.rglob("*")
            if path.is_file()
        }
        self.assertEqual(inventory, EXPECTED_ASSETS)
        joined = self.all_sources()
        self.assertNotRegex(joined, r"https?://|//cdn\.|@import|sourceMappingURL")
        self.assertNotRegex(joined, r"<script[^>]+src=[\"']https?:")
        self.assertNotIn("console.", joined)

    def test_index_has_static_landmarks_and_safe_bootstrap_order(self) -> None:
        html = self.asset("index.html")
        markers = (
            'class="skip-link" href="#main-content"',
            '<header class="app-topbar"',
            '<aside class="project-rail"',
            '<nav aria-label="Projects"',
            '<nav aria-label="Dashboard"',
            '<main id="main-content"',
            'id="route-status" class="route-status"',
            'id="global-live-region" class="sr-only" aria-live="polite"',
        )
        for marker in markers:
            with self.subTest(marker=marker):
                self.assertIn(marker, html)
        bootstrap = html.index('src="/assets/bootstrap.js"')
        tokens = html.index('href="/assets/tokens.css"')
        styles = html.index('href="/assets/dashboard.css"')
        app = html.index('type="module" src="/assets/app.js"')
        self.assertLess(bootstrap, tokens)
        self.assertLess(tokens, styles)
        self.assertLess(styles, app)
        self.assertNotRegex(html, r"<style|<script(?![^>]+src=)")

    def test_bootstrap_scrubs_one_fragment_credential_before_ready(self) -> None:
        source = self.asset("bootstrap.js")
        self.assertIn("/^#token=([A-Za-z0-9_-]{43})$/", source)
        self.assertIn("history.replaceState", source)
        self.assertIn("hydra-dashboard-ready", source)
        self.assertLess(
            source.index("history.replaceState"),
            source.index("hydra-dashboard-ready"),
        )
        self.assertIn("sessionStorage", source)
        self.assertIn("routePattern", source)
        self.assertIn("DOMContentLoaded", source)
        self.assertIn("hydra-admin-theme", source)
        self.assertIn('candidate === "light" || candidate === "dark"', source)
        self.assertNotRegex(source, r"localStorage\.setItem\([^,]*(token|credential|auth)")
        self.assertNotIn("fetch(", source)

    def test_dom_helpers_reject_unsafe_sinks_and_preserve_fact_states(self) -> None:
        joined = self.all_sources()
        for sink in (
            "innerHTML", "outerHTML", "insertAdjacentHTML", "eval(",
            "new Function",
        ):
            with self.subTest(sink=sink):
                self.assertNotIn(sink, joined)
        dom = self.asset("dom.js")
        for marker in (
            "name.startsWith(\"on\")",
            'name === "html"',
            'name === "style"',
            'name === "srcdoc"',
            "textContent",
            "document.createTextNode",
            "Number.isFinite",
            "fact.value === null",
            "fact.lower_bound",
            "fact.provenance",
            "style.flexBasis",
            'name.startsWith("aria-") ? String(value)',
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, dom)
        self.assertNotRegex(joined, r"\.srcdoc\s*=|setAttribute\([\"']srcdoc")
        self.assertNotRegex(joined, r"[\"'](?:project_id|session_id|turn_id|source_root|tool_output|worktree_path)[\"']")

    def test_fact_rendering_preserves_bounds_duration_and_provenance(self) -> None:
        dom = self.asset("dom.js")

        self.assertIn("export function factValueText", dom)
        self.assertIn("export function factPercent", dom)
        self.assertIn("formatDuration(fact.lower_bound)", dom)
        self.assertIn('fact.provenance || "provenance unavailable"', dom)
        self.assertRegex(
            dom,
            r"export function factText\(fact,[\s\S]+factAccessibleText\(fact,[\s\S]+factDetail\(fact\)",
        )
        self.assertRegex(
            dom,
            r"export function factSummaryText\(fact,[\s\S]+provenanceText\(fact\)",
        )
        self.assertNotIn("fact.lower_bound > fact.value", dom)
        self.assertIn("fact.lower_bound", dom)
        self.assertNotIn("lower bound", dom.lower())
        self.assertNotIn("upper bound", dom.lower())
        self.assertIn("≥", dom)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required to execute dashboard assets")
    def test_compact_numbers_promote_cleanly_at_suffix_boundaries(self) -> None:
        values = self.evaluate_dom(
            "[0, 999, 1000, 13500, 464000, 999499, 999500, "
            "13766276, 464000000, 1200000000, -13500]"
            ".map(dom.formatCompactNumber)"
        )

        self.assertEqual(values, [
            "0", "999", "1k", "13.5k", "464k", "999k", "1M",
            "13.8M", "464M", "1.2B", "-13.5k",
        ])

    @unittest.skipUnless(shutil.which("node"), "Node.js is required to execute dashboard assets")
    def test_fact_values_use_symbolic_non_redundant_bounds(self) -> None:
        values = self.evaluate_dom(
            "["
            "{value: 468356, lower_bound: 468356, unit: 'tokens'},"
            "{value: 500000, lower_bound: 450000, unit: 'tokens'},"
            "{value: null, lower_bound: 13500, unit: 'tokens'},"
            "{value: 0, lower_bound: 0, unit: 'tokens'},"
            "{value: null, lower_bound: 0, unit: 'tokens'},"
            "{value: null, lower_bound: null, unit: 'tokens'}"
            "].map(fact => dom.factValueText(fact))"
        )

        self.assertEqual(values, [
            "≥ 468k tokens", "500k tokens · ≥ 450k tokens",
            "≥ 13.5k tokens", "0 tokens", "Unavailable", "Unavailable",
        ])

        summary = self.evaluate_dom(
            "dom.factSummaryText({value: 1000, lower_bound: 1000, unit: 'tokens', "
            "provenance: 'derived', caveats: ['semantic_interval_allocation']})"
        )
        evidence = self.evaluate_dom(
            "dom.factText({value: 1000, lower_bound: 1000, unit: 'tokens', "
            "provenance: 'derived', caveats: ['semantic_interval_allocation']})"
        )
        self.assertEqual(summary, "≥ 1k tokens · Derived")
        self.assertEqual(
            evidence,
            "≥ 1,000 tokens · derived · semantic_interval_allocation",
        )

    @unittest.skipUnless(shutil.which("node"), "Node.js is required to execute dashboard assets")
    def test_phase_names_are_short_human_labels(self) -> None:
        labels = self.evaluate_dom(
            "['understand', 'test_targeted', 'test_full', 'browser_qa', "
            "'wait_external', 'unclassified'].map(dom.phaseDisplayName)"
        )

        self.assertEqual(labels, [
            "Understand", "Targeted tests", "Full suite", "Browser QA",
            "External wait", "Unclassified",
        ])

    def test_design_tokens_themes_and_phase_map_are_exact(self) -> None:
        tokens = self.asset("tokens.css")
        for value in (
            "#f7f8fa", "#ffffff", "#eef1f5", "#18212f", "#526071",
            "#cbd3dd", "#e5e9ef", "#1d5fd1", "#101318", "#171c23",
            "#232a34", "#edf2f7", "#b6c0cc", "#3b4654", "#29313c",
            "#7db2ff",
        ):
            with self.subTest(value=value):
                self.assertIn(value, tokens)
        self.assertIn('@media (prefers-color-scheme: dark)', tokens)
        self.assertIn(':root[data-theme="light"]', tokens)
        self.assertIn(':root[data-theme="dark"]', tokens)
        dom = self.asset("dom.js")
        for phase, color in (
            ("understand", "blue"), ("research", "blue"),
            ("design", "blue"), ("implement", "orange"),
            ("docs", "orange"), ("test_targeted", "green"),
            ("test_full", "green"), ("browser_qa", "green"),
            ("review", "purple"), ("release", "purple"),
            ("fix", "red"), ("wait_external", "neutral"),
            ("unclassified", "neutral"),
        ):
            self.assertRegex(dom, rf'{phase}["\']\s*:\s*["\']{color}')

    def test_layout_is_flat_desktop_first_and_print_safe(self) -> None:
        css = self.asset("dashboard.css")
        for marker in (
            "height: 56px", "width: 232px", "max-width: 1180px",
            "padding: 32px 36px 64px", "@media (max-width: 960px)",
            "@media (max-width: 1100px)", "@media print",
            "prefers-reduced-motion", ":focus-visible", "overflow-x: auto",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, css)
        self.assertNotRegex(css, r"@media\s*\(max-width:\s*(?:[0-8]\d\d|9[0-5]\d)px\)")
        self.assertNotRegex(css, r"gradient|backdrop-filter|box-shadow")
        self.assertNotRegex(css, r"border-radius:\s*(?:1[5-9]|[2-9]\d)px")
        self.assertIn(".metric-card", css)
        self.assertIn("border-radius: 14px", css)
        self.assertIn(".phase-track", css)
        self.assertIn("height: 20px", css)
        self.assertIn("minmax(220px, 1fr)", css)
        self.assertIn(".phase-legend li", css)
        self.assertIn("min-width: 0", css)
        self.assertIn("overflow-wrap: anywhere", css)
        self.assertIn(".project-rail", css)
        self.assertIn("display: none !important", css)
        tokens = self.asset("tokens.css")
        print_block = tokens.split("@media print", 1)[-1]
        for variable in (
            "--canvas", "--surface", "--surface-subtle", "--ink",
            "--ink-muted", "--border", "--track", "--primary",
            "--primary-soft", "--primary-foreground",
        ):
            with self.subTest(print_variable=variable):
                self.assertIn(variable, print_block)

    def test_state_and_api_keep_credentials_out_of_persistent_ui_state(self) -> None:
        state = self.asset("state.js")
        api = self.asset("api.js")
        self.assertIn("Object.freeze", state)
        self.assertNotRegex(state, r"token|credential|authorization|sessionStorage|localStorage")
        self.assertIn('Authorization', api)
        self.assertIn('Bearer ${credential}', api)
        self.assertIn("encodeURIComponent", api)
        self.assertIn("response.status === 401", api)
        self.assertIn("clearCredential", api)
        self.assertNotIn("localStorage", api)

    def test_all_routes_are_reachable_with_one_persistent_shell(self) -> None:
        app = self.asset("app.js")
        state = self.asset("state.js")
        shell = self.asset("views/shell.js")
        for route in ("overview", "tasks", "compare", "health", "evidence"):
            with self.subTest(route=route):
                self.assertIn(f'"{route}"', app + state)
                self.assertRegex(shell, rf'\["[^"]+",\s*"{route}"\]')
        self.assertIn("initializeShell", app)
        self.assertIn("updateShell", app)
        self.assertEqual(app.count("initializeShell("), 1)
        render_route = app.split("function renderRoute", 1)[-1].split("async function", 1)[0]
        self.assertNotIn("initializeShell", render_route)
        self.assertIn("routeView.replaceChildren", render_route)
        self.assertNotIn("replaceChildren", shell)
        self.assertIn("document.activeElement", app)
        self.assertIn("preventScroll", app)
        self.assertIn("aria-current", shell)
        self.assertIn("aria-pressed", self.asset("views/tasks.js"))

    def test_one_live_region_and_shared_async_states_are_used(self) -> None:
        html = self.asset("index.html")
        app = self.asset("app.js")
        dom = self.asset("dom.js")

        self.assertEqual(html.count('aria-live="polite"'), 1)
        self.assertIn('id="async-status"', html)
        self.assertIn("export function asyncState", dom)
        self.assertIn('role: "group"', dom)
        self.assertNotRegex(dom, r'role:\s*(?:"alert"|"status")')
        self.assertIn('stateKind === "notice"', dom)
        self.assertIn('"Refresh notice"', dom)
        self.assertIn("showAsyncState", app)
        self.assertIn("clearAsyncState", app)
        self.assertIn("lastAnnouncement", app)
        self.assertIn("if (message === lastAnnouncement) return", app)

    def test_overview_has_three_metrics_one_phase_figure_and_quiet_rows(self) -> None:
        source = self.asset("views/overview.js")
        self.assertEqual(source.count("metricCard("), 3)
        self.assertEqual(source.count("phaseFigure("), 1)
        for marker in (
            "Working tokens", "Full context", "Wall-clock", "Classified share",
            "Recent tasks", "Pilot", "System health", "dataTable(",
        ):
            self.assertIn(marker, source)
        self.assertIn("Latest validated task unavailable — Sync required", source)
        self.assertIn("Recent task evidence requires Sync.", source)
        self.assertNotIn("Basis: no reconciled task", source)

    def test_overview_distinguishes_unknown_false_and_true_readiness(self) -> None:
        source = self.asset("views/overview.js")

        self.assertIn('value === true ? "Ready"', source)
        self.assertIn('value === false ? "Not ready" : "Unavailable"', source)
        self.assertIn("readinessText(pilot.transport_verified)", source)
        self.assertIn("readinessText(pilot.trend_ready)", source)
        self.assertNotIn('pilot.trend_ready ? "Ready" : "Not ready"', source)

    def test_overview_distinguishes_stale_catalog_from_unstarted_pilot(self) -> None:
        source = self.asset("views/overview.js")

        for marker in (
            "function absentPilotText", 'project.freshness_state === "stale"',
            "Unavailable until Sync", "Not started", "const absentPilot",
            "pilot ? readinessText(pilot.transport_verified) : absentPilot",
            "pilot ? readinessText(pilot.trend_ready) : absentPilot",
        ):
            self.assertIn(marker, source)

    def test_tasks_compare_health_and_evidence_preserve_semantics(self) -> None:
        tasks = self.asset("views/tasks.js")
        compare = self.asset("views/compare.js")
        health = self.asset("views/health.js")
        evidence = self.asset("views/evidence.js")
        self.assertEqual(tasks.count("metricCard("), 3)
        self.assertEqual(tasks.count("phaseFigure("), 1)
        for marker in ("timeline", "test", "retry", "aria-pressed", "dataTable("):
            self.assertIn(marker, tasks)
        for marker in (
            '"Scope"', '"Failure cause"', '"Retry kind"', '"Phase"',
            '"Cause"', '"Count"', "item.scope", "item.failure_cause",
            "item.retry_kind", "item.phase", "item.cause", "item.count",
        ):
            self.assertIn(marker, tasks)
        self.assertIn("factPercent(pilot.semantic_coverage)", tasks)
        self.assertIn("Trend unavailable", tasks)
        self.assertIn("No warning detected", tasks)
        self.assertIn('el("caption"', self.asset("dom.js"))
        self.assertIn('comparison.verdict === "comparable"', compare)
        self.assertIn("Not comparable", compare)
        self.assertIn("Comparison caveats", compare)
        self.assertIn("comparison.caveats", compare)
        self.assertNotRegex(compare, r"better|worse|improved|regressed")
        self.assertIn("global launch context", health)
        self.assertNotIn("score", health.lower())
        self.assertIn("ev_[0-9a-f]{16}", evidence)
        self.assertIn("projectRef", evidence)
        self.assertNotRegex(evidence, r"appendix|download|modal")

    def test_compare_result_keeps_controls_and_attribution_on_returned_pair(self) -> None:
        compare = self.asset("views/compare.js")

        for marker in (
            "comparison.baseline_ref", "comparison.current_ref",
            "left.value = baselineRef", "right.value = currentRef",
            "!taskRefs.includes(taskRef)",
            "Baseline ${comparison.baseline_ref}",
            "Current ${comparison.current_ref}",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, compare)
        self.assertRegex(
            compare,
            r"const comparison = state\.comparison[\s\S]+left\.value = baselineRef",
        )
        self.assertNotIn("if (state.tasks[0]) left.value", compare)
        self.assertNotIn("if (state.tasks[1]) right.value", compare)

        app = self.asset("app.js")
        compare_work = app.split("async function compareTasks", 1)[-1].split(
            "async function findEvidence", 1,
        )[0]
        self.assertLess(
            compare_work.rindex("if (!isCurrentRouteWork(context)) return null;"),
            compare_work.index('dispatch({type: "comparison", comparison})'),
        )

    def test_refresh_and_navigation_keep_prior_content_during_progress(self) -> None:
        app = self.asset("app.js")
        state = self.asset("state.js")
        for stage in ("queued", "running", "succeeded", "partial", "failed"):
            self.assertIn(stage, app)
        for marker in (
            "AbortController", "sync_ref", "reused", "partial", "failed",
            "succeeded", "Retry", "aria-busy", "focus", "lastRefreshProgress",
            "reset_after_refresh", "reloadAfterRefresh",
        ):
            self.assertIn(marker, app)
        self.assertIn('case "reset_after_refresh"', state)
        self.assertIn("availableProjectRefs", state)
        self.assertIn("taskRef: null", state)
        self.assertIn("comparison: null", state)
        self.assertIn("evidence: null", state)
        self.assertNotIn("routeView.replaceChildren", app.split("startRefresh", 1)[-1])

    def test_partial_refresh_uses_human_recovery_copy(self) -> None:
        app = self.asset("app.js")

        for marker in (
            "Stable evidence remains visible",
            "Sync again",
            "A live task changed during refresh",
            "Wait for the active task to finish writing",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, app)
        terminal = app.split("async function pollRefresh", 1)[-1].split(
            "async function startRefresh", 1,
        )[0]
        self.assertIn('partial ? "notice" : "error"', terminal)
        self.assertIn("`${title}. ${detail}`", terminal)
        self.assertNotIn('diagnostic_codes || []).join(", ")', terminal)
        self.assertNotIn("Refresh partial: source_changed", app)

    def test_refresh_loading_state_reports_observed_progress_without_eta(self) -> None:
        app = self.asset("app.js")

        for marker in (
            "function refreshProgressDetail", "sources_queued",
            "sources_processed", "new_bytes", "Queued", "processed",
            "new bytes", "fact.value === 0",
        ):
            self.assertIn(marker, app)
        progress = app.split("function refreshProgressDetail", 1)[-1].split(
            "function errorMessage", 1,
        )[0]
        self.assertNotIn("%", progress)
        self.assertNotIn("ETA", progress)
        poll = app.split("async function pollRefresh", 1)[-1].split(
            "async function startRefresh", 1,
        )[0]
        visible_progress = (
            'showAsyncState("loading", "Syncing evidence", '
            "refreshProgressDetail(current))"
        )
        self.assertIn(visible_progress, poll)
        self.assertLess(poll.index(visible_progress), poll.index("announceRefresh(current)"))
        self.assertIn("announce(`Sync ${stage}`)", app)

    def test_sync_repair_polling_and_task_names_are_explicit_and_safe(self) -> None:
        app = self.asset("app.js")
        api = self.asset("api.js")
        html = self.asset("index.html")
        dom = self.asset("dom.js")
        tasks = self.asset("views/tasks.js")

        for marker in ("Sync now", "Repair history", "Start full repair", "startChangePolling", "window.setInterval(pollChanges, 1000)"):
            self.assertIn(marker, html + app)
        self.assertIn("/api/v1/changes?after=", api)
        self.assertIn("startSync", api)
        self.assertIn("startRepair", api)
        self.assertIn("export function taskDisplay", dom)
        self.assertIn("taskDisplay(task)", tasks)
        self.assertNotIn("sources_scanned", app)

    def test_task_loading_uses_the_canonical_nested_page_cursor(self) -> None:
        app = self.asset("app.js")

        self.assertIn("page.page && page.page.next_cursor", app)
        self.assertNotRegex(app, r"cursor\s*=\s*page\.next_cursor")

    def test_stale_task_routes_require_refresh_before_expensive_queries(self) -> None:
        app = self.asset("app.js")

        for marker in (
            "function routeNeedsRefresh", 'freshness === "stale"',
            'freshness === "unavailable"', 'route === "tasks"',
            'route === "compare"', "function refreshRequiredRoute",
            "Refresh Hydra before browsing task evidence.",
            "Refresh Hydra before comparing task evidence.",
        ):
            self.assertIn(marker, app)
        load_route = app.split("async function loadRoute", 1)[-1].split(
            "async function reloadAfterRefresh", 1,
        )[0]
        self.assertLess(
            load_route.index("renderRoute();"),
            load_route.index('showAsyncState("loading"'),
        )
        self.assertLess(
            load_route.index("routeNeedsRefresh"),
            load_route.index("loadAllTasks"),
        )

    def test_route_async_results_are_generation_scoped_and_cancelled(self) -> None:
        app = self.asset("app.js")

        for marker in (
            "routeWorkGeneration", "function beginRouteWork",
            "activeRequest.abort()", "function isCurrentRouteWork",
            "if (!isCurrentRouteWork(context)) return",
        ):
            self.assertIn(marker, app)
        self.assertGreaterEqual(app.count("isCurrentRouteWork(context)"), 7)
        run_async = app.split("async function runAsync", 1)[-1].split(
            "function actions", 1,
        )[0]
        self.assertLess(
            run_async.index("isCurrentRouteWork(context)"),
            run_async.index("renderError("),
        )


if __name__ == "__main__":
    unittest.main()
