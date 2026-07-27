"""Small secured route extension for persisted dashboard sync jobs."""

from __future__ import annotations

from .public_payload import reject_private_fields


def _refresh_ref(sync_ref: str) -> str:
    return "refresh_" + sync_ref.removeprefix("sync_")


def _sync_ref(refresh_ref: str) -> str:
    return "sync_" + refresh_ref.removeprefix("refresh_")


def _refresh_payload(summary: dict[str, object], reused: bool | None = None) -> dict[str, object]:
    progress = summary["progress"]
    fact = lambda value: {"value": value, "unit": "count", "provenance": "derived", "caveats": [], "lower_bound": None}
    result = {"schema_version": "hydra.dashboard-refresh/v1", "refresh_ref": _refresh_ref(str(summary["sync_ref"])),
              "state": summary["state"], "stage": "reconcile" if summary["state"] == "running" else None,
              "started_at": summary["started_at"], "finished_at": summary["finished_at"],
              "progress": {"sources_discovered": fact(progress["sources_queued"]), "sources_inspected": fact(progress["sources_processed"]),
                           "sources_scanned": fact(progress["sources_processed"]), "projects_total": fact(0),
                           "projects_completed": fact(0), "projects_refreshed": fact(0)}, "diagnostic_codes": []}
    if reused is not None: result["reused"] = reused
    return result


def payload(app: object) -> dict[str, object]:
    controller = app._sync_controller
    if controller is None:
        return {"schema_version": "hydra.dashboard-changes/v1", "data_revision": 0,
                "changed": False, "sync": {"schema_version": "hydra.dashboard-sync/v1",
                "sync_ref": None, "kind": None, "state": "idle", "started_at": None,
                "finished_at": None, "progress": {"sources_queued": 0,
                "sources_processed": 0, "new_bytes": 0}}}
    result = controller.changes(0)
    if not isinstance(result, dict) or not isinstance(result.get("data_revision"), int):
        raise ValueError("dashboard sync controller returned invalid state")
    reject_private_fields(result)
    return result


def live_snapshot_available(
    app: object, project_ref: str | None, task_ref: str | None,
) -> bool:
    """Use live reads only when the materialized launch cache is not current."""
    controller = app._sync_controller
    if controller is None:
        return False
    if app._fallback_error is not None:
        try:
            current = controller.current()
        except Exception:
            return False
        if not isinstance(current, dict) or current.get("state") not in {"succeeded", "partial"}:
            return False
    if task_ref is not None:
        return True
    selected = project_ref
    if selected is None:
        refs = app._cache.refs()
        selected = refs[0] if refs else None
    cached = None if selected is None else app._controller.snapshot(selected)
    if cached is None:
        return True
    serialized = cached.as_dict()
    revision = serialized.get("data_revision") if isinstance(serialized, dict) else None
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        return True
    return bool(controller.changes(revision)["changed"])


def dashboard_payload(app: object, result: dict[str, object]) -> dict[str, object]:
    changes = payload(app)
    result["schema_version"] = "hydra.dashboard/v2"
    revision = result.get("data_revision")
    if isinstance(revision, int) and not isinstance(revision, bool) and revision >= 0:
        current = app._sync_controller.changes(revision) if app._sync_controller is not None else changes
        result["data_revision"] = revision
        result["sync"] = current["sync"] if not current["changed"] else result.get("sync", changes["sync"])
    else:
        result["data_revision"], result["sync"] = changes["data_revision"], changes["sync"]
    reject_private_fields(result)
    return result


def dispatch(app: object, method: str, path: str, raw_query: str | None):
    """Return a response for sync routes or ``None`` when another router owns it."""
    from .dashboard_server import _HttpError, _REFRESH_REF, _SYNC_REF, _as_dict

    controller = app._sync_controller
    if path == "/api/v1/changes":
        values = app._parse_query_fields(raw_query, allowed=("after",), required=("after",))
        if method not in {"GET", "HEAD"} or not values["after"].isdigit():
            raise _HttpError("invalid_request")
        after = int(values["after"])
        if after > 9_223_372_036_854_775_807:
            raise _HttpError("invalid_request")
        if controller is None and after != 0:
            raise _HttpError("refresh_unavailable")
        result = payload(app) if after == 0 else controller.changes(after)
        return app._json(200, result, method)
    if path in {"/api/v1/sync", "/api/v1/repair"}:
        app._parse_query_fields(raw_query, allowed=())
        if method == "GET" and path == "/api/v1/sync":
            return app._json(200, payload(app)["sync"], method)
        if method != "POST" or controller is None:
            raise _HttpError("method_not_allowed")
        try:
            current, reused = (controller.start_sync() if path.endswith("sync") else controller.start_repair())
        except Exception:
            raise _HttpError("refresh_unavailable") from None
        result = _as_dict(type("_Payload", (), {"as_dict": lambda _self: current})())
        result["reused"] = bool(reused)
        return app._json(202, result, method, extra=(("Location", f"/api/v1/sync/{result['sync_ref']}"),))
    if path.startswith("/api/v1/sync/"):
        app._parse_query_fields(raw_query, allowed=())
        if method not in {"GET", "HEAD"} or controller is None:
            raise _HttpError("method_not_allowed")
        sync_ref = app._reference(path.removeprefix("/api/v1/sync/"), _SYNC_REF)
        assert sync_ref is not None
        return app._json(200, controller.get(sync_ref), method)
    if path == "/api/v1/refresh" and controller is not None:
        app._parse_query_fields(raw_query, allowed=())
        if method != "POST":
            raise _HttpError("method_not_allowed")
        try:
            current, reused = controller.start_sync()
        except Exception:
            raise _HttpError("refresh_unavailable") from None
        result = _refresh_payload(current, bool(reused))
        return app._json(202, result, method, extra=(("Location", f"/api/v1/refresh/{result['refresh_ref']}"),))
    if path.startswith("/api/v1/refresh/") and controller is not None:
        app._parse_query_fields(raw_query, allowed=())
        if method not in {"GET", "HEAD"}:
            raise _HttpError("method_not_allowed")
        refresh_ref = app._reference(path.removeprefix("/api/v1/refresh/"), _REFRESH_REF)
        assert refresh_ref is not None
        return app._json(200, _refresh_payload(controller.get(_sync_ref(refresh_ref))), method)
    return None
