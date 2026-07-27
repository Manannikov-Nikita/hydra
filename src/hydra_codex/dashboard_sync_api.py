"""Small secured route extension for persisted dashboard sync jobs."""

from __future__ import annotations

from .public_payload import reject_private_fields


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


def dashboard_payload(app: object, result: dict[str, object]) -> dict[str, object]:
    changes = payload(app)
    result["schema_version"] = "hydra.dashboard/v2"
    result["data_revision"], result["sync"] = changes["data_revision"], changes["sync"]
    reject_private_fields(result)
    return result


def dispatch(app: object, method: str, path: str, raw_query: str | None):
    """Return a response for sync routes or ``None`` when another router owns it."""
    from .dashboard_server import _HttpError, _SYNC_REF, _as_dict

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
        return app._json(202, {**current, "reused": bool(reused)}, method,
                         extra=(("Location", f"/api/v1/sync/{current['sync_ref']}"),))
    if path.startswith("/api/v1/refresh/") and controller is not None:
        app._parse_query_fields(raw_query, allowed=())
        if method not in {"GET", "HEAD"}:
            raise _HttpError("method_not_allowed")
        sync_ref = app._reference(path.removeprefix("/api/v1/refresh/"), _SYNC_REF)
        assert sync_ref is not None
        return app._json(200, controller.get(sync_ref), method)
    return None
