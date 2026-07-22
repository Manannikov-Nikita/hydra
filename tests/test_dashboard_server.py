from __future__ import annotations

from contextlib import redirect_stderr
from dataclasses import FrozenInstanceError
import http.client
import io
import json
from pathlib import Path
import socket
import sqlite3
import struct
import threading
import unittest

from hydra_codex.dashboard_server import (
    DashboardApplication,
    DashboardAsset,
    DashboardRequest,
    DashboardResponse,
    create_dashboard_server,
)
from hydra_codex.storage import StorageUnavailable


TOKEN = "secret-dashboard-token"
AUTHORITY = "127.0.0.1:43123"
PROJECT = "project_0123456789ab"
TASK_A = "task_0123456789ab"
TASK_B = "task_abcdef012345"
REFRESH = "refresh_0123456789ab"
EVIDENCE = "ev_0123456789abcdef"

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; "
        "connect-src 'self'; img-src 'self' data:; font-src 'none'; "
        "object-src 'none'; base-uri 'none'; form-action 'none'; "
        "frame-ancestors 'none'; manifest-src 'none'; worker-src 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Connection": "close",
    "Server": "Hydra",
}


class PublicPayload:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def as_dict(self) -> dict[str, object]:
        return self.payload


class FakeQueryService:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.failure: Exception | None = None

    def _result(self, name: str, payload: dict[str, object]) -> PublicPayload:
        self.calls.append((name,))
        if self.failure is not None:
            raise self.failure
        return PublicPayload(payload)

    def snapshot(self, *, project_ref, task_ref, refresh):
        self.calls.append(("snapshot", project_ref, task_ref, refresh))
        if self.failure is not None:
            raise self.failure
        return PublicPayload({
            "schema_version": "hydra.dashboard/v1",
            "selected_project_ref": project_ref,
            "selected_task": task_ref,
            "refresh": refresh.as_dict(),
        })

    def tasks(self, project_ref, *, cursor, limit=50):
        self.calls.append(("tasks", project_ref, cursor, limit))
        if self.failure is not None:
            raise self.failure
        return PublicPayload({
            "schema_version": "hydra.dashboard-task-list/v1",
            "project_ref": project_ref,
            "cursor": cursor,
            "limit": limit,
        })

    def compare(self, project_ref, left, right):
        self.calls.append(("compare", project_ref, left, right))
        if self.failure is not None:
            raise self.failure
        return PublicPayload({
            "schema_version": "hydra.comparison/v2",
            "project_ref": project_ref,
            "baseline_ref": left,
            "current_ref": right,
        })

    def evidence(self, project_ref, evidence_id):
        self.calls.append(("evidence", project_ref, evidence_id))
        if self.failure is not None:
            raise self.failure
        return PublicPayload({
            "evidence_id": evidence_id,
            "fact": "tokens.working",
            "project_ref": project_ref,
        })


class FakeRefreshSnapshot:
    def __init__(self, state: str = "running") -> None:
        self.refresh_ref = REFRESH
        self.state = state

    def as_dict(self) -> dict[str, object]:
        return {
            "refresh_ref": self.refresh_ref,
            "state": self.state,
            "stage": "scan" if self.state == "running" else None,
            "started_at": "2026-07-22T00:00:00Z",
            "finished_at": None,
            "progress": {},
            "diagnostic_codes": [],
        }

    def to_view(self) -> PublicPayload:
        return PublicPayload(self.as_dict())


class FakeRefreshController:
    def __init__(self) -> None:
        self.starts = 0
        self.gets: list[str] = []
        self.snapshots: dict[str, PublicPayload] = {
            PROJECT: PublicPayload({
                "schema_version": "hydra.dashboard/v1",
                "selected_project_ref": PROJECT,
                "source": "last-valid-cache",
            }),
        }
        self.refresh = FakeRefreshSnapshot()
        self.close_calls = 0

    def start(self):
        self.starts += 1
        return self.refresh, self.starts > 1

    def get(self, refresh_ref):
        self.gets.append(refresh_ref)
        if refresh_ref != REFRESH:
            raise KeyError("private selector")
        return self.refresh

    def current(self):
        return self.refresh

    def snapshot(self, project_ref):
        return self.snapshots.get(project_ref)

    def close(self):
        self.close_calls += 1


class FakeCache:
    def __init__(self, refs: tuple[str, ...] = (PROJECT,)) -> None:
        self._refs = refs

    def refs(self):
        return self._refs


def request(
    target: str,
    *,
    method: str = "GET",
    host: str | None = AUTHORITY,
    auth: str | None = TOKEN,
    origin: str | None = None,
    extra: tuple[tuple[str, str], ...] = (),
    body: bytes = b"",
) -> DashboardRequest:
    headers: list[tuple[str, str]] = []
    if host is not None:
        headers.append(("Host", host))
    if auth is not None:
        headers.append(("Authorization", f"Bearer {auth}"))
    if origin is not None:
        headers.append(("Origin", origin))
    headers.extend(extra)
    return DashboardRequest(method, target, tuple(headers), body)


class DashboardApplicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.query = FakeQueryService()
        self.controller = FakeRefreshController()
        self.cache = FakeCache()
        self.assets = {
            "/": DashboardAsset("text/html; charset=utf-8", b"<!doctype html>"),
            "/assets/app.js": DashboardAsset("text/javascript; charset=utf-8", b"ok();"),
        }
        self.unbound = DashboardApplication(
            token=TOKEN,
            query_service=self.query,
            refresh_controller=self.controller,
            snapshot_cache=self.cache,
            assets=self.assets,
        )
        self.app = self.unbound.bound_to(AUTHORITY)

    @staticmethod
    def payload(response: DashboardResponse) -> dict[str, object]:
        return json.loads(response.body)

    def assert_safe(self, response: DashboardResponse, *, length: int | None = None) -> None:
        headers = dict(response.headers)
        for name, value in SECURITY_HEADERS.items():
            self.assertEqual(headers.get(name), value)
        self.assertFalse(any(name.lower().startswith("access-control-") for name in headers))
        expected = len(response.body) if length is None else length
        self.assertEqual(headers["Content-Length"], str(expected))

    def test_contracts_are_frozen_and_secrets_are_absent_from_repr(self) -> None:
        contracts = (
            DashboardRequest("GET", "/", (("Authorization", TOKEN),), TOKEN.encode()),
            DashboardResponse(200, (("X-Test", "ok"),), b"ok"),
            DashboardAsset("text/plain", TOKEN.encode()),
        )
        for value, field_name in zip(contracts, ("method", "status", "content_type"), strict=True):
            with self.subTest(value=type(value).__name__):
                with self.assertRaises(FrozenInstanceError):
                    setattr(value, field_name, "changed")
        self.assertNotIn(TOKEN, repr(contracts[0]))
        self.assertNotIn(TOKEN, repr(contracts[2]))
        self.assertNotIn(TOKEN, repr(self.unbound))
        self.assertIsNot(self.app, self.unbound)
        self.assertEqual(self.unbound.handle(request("/")).status, 500)
        with self.assertRaises(ValueError):
            self.unbound.bound_to("localhost:43123")
        source = Path(__file__).parents[1] / "src/hydra_codex/dashboard_server.py"
        self.assertLess(len(source.read_text(encoding="utf-8").splitlines()), 500)

    def test_exact_static_assets_and_head_parity(self) -> None:
        get = self.app.handle(request("/", auth=None))
        head = self.app.handle(request("/", method="HEAD", auth=None))
        self.assertEqual((get.status, get.body), (200, b"<!doctype html>"))
        self.assertEqual(head.status, 200)
        self.assertEqual(head.body, b"")
        self.assertEqual(dict(head.headers)["Content-Length"], str(len(get.body)))
        self.assertEqual(
            self.app.handle(request("/assets/app.js", auth=None)).body, b"ok();",
        )
        self.assertEqual(self.app.handle(request("/missing", auth=None)).status, 404)
        self.assert_safe(get)
        self.assert_safe(head, length=len(get.body))

    def test_host_origin_and_bearer_are_exact_and_unique(self) -> None:
        target = f"/api/v1/snapshot?project={PROJECT}"
        ok = self.app.handle(request(target, origin=f"http://{AUTHORITY}"))
        self.assertEqual(ok.status, 200)
        cases = (
            (request(target, host=None), 400),
            (request(target, host="localhost:43123"), 400),
            (request(target, host=AUTHORITY + "."), 400),
            (request(target, extra=(("HOST", AUTHORITY),)), 400),
            (request(target, origin="https://evil.invalid"), 403),
            (request(target, origin=f"http://{AUTHORITY}/"), 403),
            (request(target, origin="null"), 403),
            (request(target, origin=f"http://[::1]:43123"), 403),
            (request(target, extra=(("Origin", f"http://{AUTHORITY}"),
                                    ("Origin", f"http://{AUTHORITY}"))), 403),
            (request(target, auth=None), 401),
            (request(target, auth="wrong"), 401),
            (request(target, extra=(("Authorization", f"Bearer {TOKEN}"),)), 401),
            (request(target, extra=(("Authorization", TOKEN),)), 401),
        )
        for candidate, status in cases:
            with self.subTest(status=status, candidate=repr(candidate)):
                response = self.app.handle(candidate)
                self.assertEqual(response.status, status)
                self.assert_safe(response)
                if status == 401:
                    self.assertEqual(dict(response.headers)["WWW-Authenticate"], "Bearer")

    def test_header_target_and_framing_bounds_fail_before_dispatch(self) -> None:
        target = f"/api/v1/snapshot?project={PROJECT}"
        too_many = tuple((f"X-{index}", "x") for index in range(63))
        cases = (
            request(target, extra=(("Transfer-Encoding", "chunked"),)),
            request(target, extra=(("Content-Encoding", "gzip"),)),
            request(target, extra=(("Content-Length", "1"),)),
            request(target, extra=(("Content-Length", "0"), ("Content-Length", "0"))),
            request(target, extra=(("Content-Length", "+0"),)),
            request(target, body=b"x"),
            request(target, extra=(("X-Bad", "line\nfeed"),)),
            request(target, extra=(("X-Surrogate", "\ud800"),)),
            request(target, extra=(("X-Huge", "x" * 8193),)),
            request(target, extra=tuple((f"X-Aggregate-{index}", "x" * 7000)
                                        for index in range(5))),
            request(target, extra=too_many),
            request("/" + "x" * 2048, auth=None),
            request("https://127.0.0.1/", auth=None),
            request("//assets/app.js", auth=None),
            request("/assets/%61pp.js", auth=None),
            request("/assets/../app.js", auth=None),
            request("/assets\\app.js", auth=None),
            request("/assets/app.js#fragment", auth=None),
            DashboardRequest("GET", "/", [("Host", AUTHORITY)]),
        )
        for candidate in cases:
            with self.subTest(candidate=repr(candidate)):
                response = self.app.handle(candidate)
                self.assertEqual(response.status, 400)
                self.assert_safe(response)
        self.assertEqual(self.query.calls, [])
        boundary = self.app.handle(request("/" + "x" * 2047, auth=None))
        self.assertEqual(boundary.status, 404)
        accepted_zero = self.app.handle(request(
            "/", auth=None, extra=(("Content-Length", "0" * 8192),),
        ))
        self.assertEqual(accepted_zero.status, 200)

    def test_validation_order_is_stable_and_does_not_parse_untrusted_selectors_early(self) -> None:
        malformed = f"/api/v1/tasks?project={PROJECT}&extra={TOKEN}"
        cases = (
            (request(malformed, host="localhost:1", auth=None), 400),
            (request(malformed, origin="https://evil.invalid", auth=None), 403),
            (request(malformed, auth=None), 401),
            (request(malformed), 400),
            (request(malformed, method="PUT"), 400),
            (request(f"/api/v1/tasks?project={PROJECT}", method="PUT"), 405),
        )
        for candidate, expected in cases:
            with self.subTest(expected=expected):
                response = self.app.handle(candidate)
                self.assertEqual(response.status, expected)
                self.assertNotIn(TOKEN.encode(), response.body)

    def test_query_grammar_and_exact_service_arguments(self) -> None:
        calls = (
            (f"/api/v1/snapshot?project={PROJECT}&task={TASK_A}",
             ("snapshot", PROJECT, TASK_A)),
            (f"/api/v1/tasks?project={PROJECT}&cursor={TASK_A}&limit=17",
             ("tasks", PROJECT, TASK_A, 17)),
            (f"/api/v1/compare?project={PROJECT}&left={TASK_A}&right={TASK_B}",
             ("compare", PROJECT, TASK_A, TASK_B)),
            (f"/api/v1/evidence/{EVIDENCE}?project={PROJECT}",
             ("evidence", PROJECT, EVIDENCE)),
        )
        for target, expected in calls:
            with self.subTest(target=target):
                before = len(self.query.calls)
                response = self.app.handle(request(target))
                self.assertEqual(response.status, 200)
                self.assertEqual(self.query.calls[before][:len(expected)], expected)
                self.assert_safe(response)
        bad = (
            f"/api/v1/snapshot?task={TASK_A}",
            f"/api/v1/tasks?project={PROJECT}&project={PROJECT}",
            f"/api/v1/tasks?project={PROJECT}&limit=0",
            f"/api/v1/tasks?project={PROJECT}&limit=101",
            f"/api/v1/tasks?project={PROJECT}&extra=x",
            f"/api/v1/compare?project={PROJECT}&left={TASK_A}",
            f"/api/v1/evidence/ev_bad?project={PROJECT}",
            f"/api/v1/evidence/{EVIDENCE}?project=",
            "/api/v1/refresh?extra=x",
            f"/api/v1/refresh/{REFRESH}?extra=x",
        )
        for target in bad:
            with self.subTest(target=target):
                self.assertEqual(self.app.handle(request(target)).status, 400)

    def test_no_task_snapshot_uses_last_valid_cache_with_refresh_overlay_seam(self) -> None:
        response = self.app.handle(request(f"/api/v1/snapshot?project={PROJECT}"))
        self.assertEqual(response.status, 200)
        self.assertEqual(self.payload(response)["source"], "last-valid-cache")
        self.assertFalse(any(call[0] == "snapshot" for call in self.query.calls))
        default = self.app.handle(request("/api/v1/snapshot"))
        self.assertEqual(default.status, 200)
        self.assertEqual(self.payload(default)["selected_project_ref"], PROJECT)
        self.controller.snapshots.clear()
        self.assertEqual(
            self.app.handle(request(f"/api/v1/snapshot?project={PROJECT}")).status,
            404,
        )

    def test_empty_cache_never_reads_live_storage_or_crosses_publication_barrier(self) -> None:
        empty = DashboardApplication(
            token=TOKEN, query_service=self.query,
            refresh_controller=self.controller, snapshot_cache=FakeCache(()),
            assets=self.assets,
        ).bound_to(AUTHORITY)
        self.controller.snapshots.clear()
        response = empty.handle(request("/api/v1/snapshot"))
        self.assertEqual(response.status, 503)
        self.assertEqual(self.payload(response)["error"]["code"], "refresh_unavailable")
        self.assertEqual(self.query.calls, [])

    def test_refresh_start_and_status_have_safe_schema_and_location(self) -> None:
        post = request(
            "/api/v1/refresh", method="POST", origin=f"http://{AUTHORITY}",
            extra=(("Content-Length", "0"),),
        )
        response = self.app.handle(post)
        self.assertEqual(response.status, 202)
        self.assertEqual(dict(response.headers)["Location"], f"/api/v1/refresh/{REFRESH}")
        self.assertEqual(self.payload(response)["schema_version"], "hydra.dashboard-refresh/v1")
        self.assertFalse(self.payload(response)["reused"])
        status = self.app.handle(request(f"/api/v1/refresh/{REFRESH}"))
        self.assertEqual(status.status, 200)
        self.assertEqual(self.payload(status)["refresh_ref"], REFRESH)
        rejected = self.app.handle(request(
            "/api/v1/refresh", method="POST", origin=f"http://{AUTHORITY}",
            extra=(("Content-Length", "2"),), body=b"{}",
        ))
        self.assertEqual(rejected.status, 400)
        self.assertEqual(self.controller.starts, 1)
        original = self.controller.start
        self.controller.start = lambda: (_ for _ in ()).throw(RuntimeError(TOKEN))
        unavailable = self.app.handle(post)
        self.assertEqual(unavailable.status, 503)
        self.assertEqual(self.payload(unavailable)["error"]["code"], "refresh_unavailable")
        self.controller.start = original

    def test_errors_are_categorical_secret_free_and_head_safe(self) -> None:
        target = f"/api/v1/tasks?project={PROJECT}"
        failures = (
            (KeyError(TOKEN), 404, "not_found"),
            (StorageUnavailable(TOKEN), 503, "storage_unavailable"),
            (ValueError(TOKEN), 500, "internal_failure"),
            (RuntimeError(TOKEN), 500, "internal_failure"),
        )
        for failure, status, code in failures:
            with self.subTest(failure=type(failure).__name__):
                self.query.failure = failure
                response = self.app.handle(request(target, method="HEAD"))
                self.assertEqual(response.status, status)
                self.assertEqual(response.body, b"")
                self.assertNotIn(TOKEN, repr(response.headers))
                self.assertNotIn(TOKEN.encode(), response.body)
                get = self.app.handle(request(target))
                self.assertEqual(self.payload(get)["error"]["code"], code)
                self.assertNotIn(TOKEN.encode(), get.body)
                self.assert_safe(response, length=len(get.body))
        busy = sqlite3.OperationalError(TOKEN)
        busy.sqlite_errorcode = sqlite3.SQLITE_BUSY
        self.query.failure = busy
        self.assertEqual(self.app.handle(request(target)).status, 503)
        other = sqlite3.OperationalError(TOKEN)
        other.sqlite_errorcode = sqlite3.SQLITE_IOERR
        self.query.failure = other
        self.assertEqual(self.app.handle(request(target)).status, 500)
        self.query.failure = None
        self.query.tasks = lambda *_args, **_kwargs: PublicPayload({
            "project_id": TOKEN,
        })
        rejected_private = self.app.handle(request(target))
        self.assertEqual(rejected_private.status, 500)
        self.assertNotIn(TOKEN.encode(), rejected_private.body)

    def test_unsupported_methods_and_options_are_json_without_cors(self) -> None:
        for method in ("OPTIONS", "PUT", "PATCH", "DELETE", "TRACE", "CONNECT"):
            with self.subTest(method=method):
                response = self.app.handle(request("/api/v1/snapshot", method=method))
                self.assertEqual(response.status, 405)
                self.assertEqual(
                    self.payload(response)["error"]["code"], "method_not_allowed",
                )
                self.assert_safe(response)
        preflight = self.app.handle(request(
            "/api/v1/snapshot", method="OPTIONS", auth=None,
            origin=f"http://{AUTHORITY}",
        ))
        self.assertEqual(preflight.status, 405)


class DashboardAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.query = FakeQueryService()
        self.controller = FakeRefreshController()
        self.app = DashboardApplication(
            token=TOKEN,
            query_service=self.query,
            refresh_controller=self.controller,
            snapshot_cache=FakeCache(),
            assets={"/": DashboardAsset("text/plain; charset=utf-8", b"Hydra")},
        )

    def test_port_zero_loopback_duplicate_headers_and_bounded_shutdown(self) -> None:
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            server = create_dashboard_server(port=0, application=self.app)
            host, port = server.server_address
            self.assertEqual(host, "127.0.0.1")
            self.assertIsInstance(port, int)
            self.assertGreater(port, 0)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                connection = http.client.HTTPConnection(host, port, timeout=2)
                connection.request("GET", "/")
                response = connection.getresponse()
                self.assertEqual((response.status, response.read()), (200, b"Hydra"))
                self.assertEqual(response.getheader("Server"), "Hydra")
                self.assertNotIn("Python", repr(response.getheaders()))
                connection.close()

                extension = http.client.HTTPConnection(host, port, timeout=2)
                extension.request("BREW", "/")
                unsupported = extension.getresponse()
                self.assertEqual(unsupported.status, 405)
                unsupported.read()
                extension.close()

                duplicate = http.client.HTTPConnection(host, port, timeout=2)
                duplicate.putrequest("GET", "/", skip_host=True)
                duplicate.putheader("Host", f"{host}:{port}")
                duplicate.putheader("Host", f"{host}:{port}")
                duplicate.endheaders()
                duplicate_response = duplicate.getresponse()
                self.assertEqual(duplicate_response.status, 400)
                self.assertNotIn(b"<!DOCTYPE", duplicate_response.read())
                duplicate.close()

                no_body = http.client.HTTPConnection(host, port, timeout=2)
                no_body.putrequest("POST", "/api/v1/refresh", skip_host=True)
                no_body.putheader("Host", f"{host}:{port}")
                no_body.putheader("Origin", f"http://{host}:{port}")
                no_body.putheader("Authorization", f"Bearer {TOKEN}")
                no_body.putheader("Content-Length", "999999")
                no_body.endheaders()
                rejected = no_body.getresponse()
                self.assertEqual(rejected.status, 400)
                rejected.read()
                no_body.close()

                reset = socket.create_connection((host, port), timeout=2)
                reset.setsockopt(
                    socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0),
                )
                reset.sendall(
                    f"GET / HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode("ascii"),
                )
                reset.close()
                threading.Event().wait(0.1)

                partial = socket.create_connection((host, port), timeout=2)
                partial.setsockopt(
                    socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0),
                )
                partial.sendall(b"G")
                partial.close()
                threading.Event().wait(0.1)

                pre_request = socket.create_connection((host, port), timeout=2)
                pre_request.setsockopt(
                    socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0),
                )
                pre_request.close()
                threading.Event().wait(0.1)
            finally:
                server.shutdown()
                server.server_close()
                worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.controller.close_calls, 0)
        self.assertEqual(stderr.getvalue(), "")

    def test_malformed_http_never_uses_stdlib_html_error_page(self) -> None:
        server = create_dashboard_server(port=0, application=self.app)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            with socket.create_connection(server.server_address, timeout=2) as client:
                client.sendall(b"BROKEN\r\n\r\n")
                payload = client.recv(8192)
            self.assertIn(b"400", payload)
            self.assertIn(b"hydra.dashboard-error/v1", payload)
            self.assertNotIn(b"<!DOCTYPE", payload)
            self.assertNotIn(b"BaseHTTP", payload)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(2)


if __name__ == "__main__":
    unittest.main()
