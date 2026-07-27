from __future__ import annotations

import json
import unittest

from hydra_codex.dashboard_server import DashboardApplication, DashboardAsset, DashboardRequest


TOKEN = "dashboard-token"
AUTHORITY = "127.0.0.1:43123"


class Query:
    def snapshot(self, **_kwargs):
        return type("Payload", (), {"as_dict": lambda _self: {
            "schema_version": "hydra.dashboard/v1", "selected_project_ref": None,
        }})()


class Cache:
    def refs(self):
        return ()


class Refresh:
    def current(self):
        return None

    def succeeded_once(self):
        return True


class Sync:
    def __init__(self) -> None:
        self.started: list[str] = []

    @staticmethod
    def current():
        return {"schema_version": "hydra.dashboard-sync/v1", "sync_ref": "sync_0123456789abcdef",
                "kind": "sync", "state": "queued", "started_at": "2026-07-27T10:00:00Z",
                "finished_at": None, "progress": {"sources_queued": 1, "sources_processed": 0, "new_bytes": 0}}

    def changes(self, after):
        return {"schema_version": "hydra.dashboard-changes/v1", "data_revision": 4,
                "changed": 4 > after, "sync": self.current()}

    def start_sync(self):
        self.started.append("sync")
        return self.current(), False

    def start_repair(self):
        self.started.append("repair")
        value = self.current() | {"kind": "repair", "sync_ref": "sync_abcdef0123456789"}
        return value, False

    def get(self, sync_ref):
        if sync_ref != "sync_0123456789abcdef":
            raise KeyError(sync_ref)
        return self.current()


class DashboardSyncApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sync = Sync()
        self.app = DashboardApplication(
            token=TOKEN, query_service=Query(), refresh_controller=Refresh(),
            sync_controller=self.sync, snapshot_cache=Cache(),
            assets={"/": DashboardAsset("text/html", b"ok")},
        ).bound_to(AUTHORITY)

    def request(self, target, method="GET", origin=None):
        headers = (("Host", AUTHORITY), ("Authorization", f"Bearer {TOKEN}"))
        if origin is not None:
            headers += (("Origin", origin),)
        return self.app.handle(DashboardRequest(method, target, headers, b""))

    def payload(self, response):
        return json.loads(response.body)

    def test_changes_and_persisted_sync_are_authenticated_and_revision_safe(self) -> None:
        response = self.request("/api/v1/changes?after=3")
        self.assertEqual(response.status, 200)
        payload = self.payload(response)
        self.assertEqual((payload["changed"], payload["data_revision"]), (True, 4))
        self.assertEqual(payload["sync"]["sync_ref"], "sync_0123456789abcdef")
        self.assertEqual(self.request("/api/v1/changes?after=-1").status, 400)
        self.assertEqual(self.request("/api/v1/changes?after=3", origin="https://invalid").status, 403)

    def test_sync_repair_and_refresh_alias_keep_safe_single_flight_contract(self) -> None:
        origin = f"http://{AUTHORITY}"
        sync = self.request("/api/v1/sync", "POST", origin)
        repair = self.request("/api/v1/repair", "POST", origin)
        alias = self.request("/api/v1/refresh", "POST", origin)
        self.assertEqual((sync.status, repair.status, alias.status), (202, 202, 202))
        self.assertEqual(self.sync.started, ["sync", "repair", "sync"])
        self.assertEqual(self.payload(alias)["schema_version"], "hydra.dashboard-sync/v1")
        self.assertEqual(self.request("/api/v1/sync/sync_0123456789abcdef").status, 200)

