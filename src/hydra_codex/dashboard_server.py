"""Pure secured loopback dashboard application and stdlib HTTP adapter."""
from __future__ import annotations
from collections.abc import Mapping
from dataclasses import dataclass, field
import json, re, secrets, sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import MappingProxyType
from typing import Any
from .dashboard_model import DashboardRefreshView
from .dashboard_sync_api import dashboard_payload, dispatch as dispatch_sync, live_snapshot_available
from .public_payload import reject_private_fields
from .storage import StorageUnavailable
_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; "
    "connect-src 'self'; img-src 'self' data:; font-src 'none'; "
    "object-src 'none'; base-uri 'none'; form-action 'none'; "
    "frame-ancestors 'none'; manifest-src 'none'; worker-src 'none'"
)
_SECURITY_HEADERS = (
    ("Content-Security-Policy", _CSP),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("Cache-Control", "no-store"),
    ("X-Frame-Options", "DENY"),
    ("Cross-Origin-Opener-Policy", "same-origin"),
    ("Cross-Origin-Resource-Policy", "same-origin"),
    ("Server", "Hydra"),
)
_ERROR_STATUS = {
    "invalid_request": 400, "unauthorized": 401, "forbidden_origin": 403,
    "not_found": 404, "method_not_allowed": 405, "storage_unavailable": 503,
    "database_busy": 503, "refresh_unavailable": 503, "internal_failure": 500,
}
_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}\Z")
_AUTHORITY = re.compile(r"127\.0\.0\.1:([1-9][0-9]{0,4})\Z")
_PROJECT_REF, _TASK_REF = re.compile(r"project_[0-9a-f]{12,64}\Z"), re.compile(r"task_[0-9a-f]{1,64}\Z")
_REFRESH_REF, _EVIDENCE_REF = re.compile(r"refresh_[0-9a-f]{12,64}\Z"), re.compile(r"ev_[0-9a-f]{16}\Z")
_SYNC_REF = re.compile(r"sync_[0-9a-f]{12,64}\Z")
_MAX_HEADERS, _MAX_HEADER_VALUE, _MAX_HEADER_BYTES, _MAX_TARGET_BYTES = 64, 8192, 32768, 2048
@dataclass(frozen=True)
class DashboardRequest:
    method: str
    target: str
    headers: tuple[tuple[str, str], ...] = field(default=(), repr=False)
    body: bytes = field(default=b"", repr=False)
@dataclass(frozen=True)
class DashboardResponse:
    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes
@dataclass(frozen=True)
class DashboardAsset:
    content_type: str
    body: bytes = field(repr=False)
    def __post_init__(self) -> None:
        if (
            not isinstance(self.content_type, str)
            or not self.content_type
            or not self.content_type.isascii()
            or any(ord(character) < 32 or ord(character) == 127 for character in self.content_type)
        ):
            raise ValueError("asset content type must be safe text")
        if not isinstance(self.body, bytes):
            raise TypeError("asset body must be bytes")
class _HttpError(Exception):
    def __init__(self, code: str) -> None:
        if code not in _ERROR_STATUS:
            raise ValueError("unsupported dashboard error code")
        self.code = code
        super().__init__(code)
def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
    ).encode("utf-8")
def _as_dict(value: object) -> dict[str, object]:
    method = getattr(value, "as_dict", None)
    if not callable(method):
        raise ValueError("dashboard service returned an invalid public contract")
    payload = method()
    if not isinstance(payload, dict):
        raise ValueError("dashboard public contract must serialize to an object")
    reject_private_fields(payload)
    return payload
class DashboardApplication:
    def __init__(
        self, *, token: str, query_service: object, refresh_controller: object,
        sync_controller: object | None = None,
        snapshot_cache: object, assets: Mapping[str, DashboardAsset],
        fallback_snapshot: object | None = None, fallback_error: str | None = None,
        _authority: str | None = None,
    ) -> None:
        if (
            not isinstance(token, str) or not token or len(token) > 512
            or any(ord(character) < 33 or ord(character) > 126 for character in token)
        ):
            raise ValueError("dashboard token must be non-empty ASCII text")
        if not isinstance(assets, Mapping):
            raise TypeError("dashboard assets must be a mapping")
        copied: dict[str, DashboardAsset] = {}
        for path, asset in assets.items():
            if (
                not isinstance(path, str)
                or (path != "/" and not path.startswith("/assets/"))
                or any(character in path for character in ("%", "\\", "#", "?"))
                or "//" in path or "/./" in path or "/../" in path
                or path.endswith("/") and path != "/"
                or any(segment in {".", ".."} for segment in path.split("/"))
            ):
                raise ValueError("asset paths must be exact safe origin paths")
            if not isinstance(asset, DashboardAsset):
                raise TypeError("asset values must be DashboardAsset")
            copied[path] = asset
        if _authority is not None:
            self._validate_authority(_authority)
        if fallback_error not in {None, "storage_unavailable"}: raise ValueError("dashboard fallback error is invalid")
        self._token, self._query, self._controller = token, query_service, refresh_controller
        self._sync_controller = sync_controller
        self._cache, self._fallback_snapshot, self._fallback_error = snapshot_cache, fallback_snapshot, fallback_error
        self._assets, self._authority = MappingProxyType(dict(sorted(copied.items()))), _authority
    @staticmethod
    def _validate_authority(authority: str) -> None:
        match = _AUTHORITY.fullmatch(authority) if isinstance(authority, str) else None
        if match is None or int(match.group(1)) > 65535:
            raise ValueError("dashboard authority must be numeric IPv4 loopback")
    def bound_to(self, authority: str) -> DashboardApplication:
        self._validate_authority(authority)
        return DashboardApplication(
            token=self._token, query_service=self._query,
            refresh_controller=self._controller, sync_controller=self._sync_controller, snapshot_cache=self._cache,
            assets=self._assets, fallback_snapshot=self._fallback_snapshot,
            fallback_error=self._fallback_error, _authority=authority,
        )
    def __repr__(self) -> str:
        return f"DashboardApplication(bound={self._authority is not None}, asset_count={len(self._assets)})"
    def handle(self, request: DashboardRequest) -> DashboardResponse:
        method = request.method if isinstance(request, DashboardRequest) and isinstance(request.method, str) else "GET"
        try:
            if not isinstance(request, DashboardRequest):
                raise _HttpError("invalid_request")
            self._validate_request_shape(request)
            if self._authority is None:
                raise RuntimeError("dashboard application is not bound")
            target = self._validate_target(request.target)
            headers = self._header_map(request.headers)
            self._validate_host(headers)
            self._validate_origin(headers, request.method)
            self._validate_framing(headers, request.body)
            if request.method == "OPTIONS":
                raise _HttpError("method_not_allowed")
            is_api = target == "/api/v1" or target.startswith(("/api/v1/", "/api/v1?"))
            if is_api:
                self._validate_bearer(headers)
            path, query = self._parse_target(target)
            return self._dispatch(request.method, path, query)
        except _HttpError as error:
            return self._error(error.code, method)
        except KeyError:
            return self._error("not_found", method)
        except StorageUnavailable:
            return self._error("storage_unavailable", method)
        except sqlite3.OperationalError as error:
            code = getattr(error, "sqlite_errorcode", None)
            if isinstance(code, int) and code & 0xFF in {
                sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED,
            }:
                return self._error("database_busy", method)
            return self._error("internal_failure", method)
        except Exception:
            return self._error("internal_failure", method)
    @staticmethod
    def _validate_request_shape(request: DashboardRequest) -> None:
        if (
            not isinstance(request.method, str)
            or not request.method
            or len(request.method) > 16
            or not request.method.isascii()
            or not request.method.isupper()
            or not isinstance(request.target, str)
            or not isinstance(request.headers, tuple)
            or not isinstance(request.body, bytes)
        ):
            raise _HttpError("invalid_request")
        if len(request.headers) > _MAX_HEADERS:
            raise _HttpError("invalid_request")
        total = 0
        for item in request.headers:
            if not isinstance(item, tuple) or len(item) != 2:
                raise _HttpError("invalid_request")
            name, value = item
            try:
                value_size = len(value.encode("utf-8")) if isinstance(value, str) else -1
            except UnicodeEncodeError:
                raise _HttpError("invalid_request") from None
            if (
                not isinstance(name, str) or _HEADER_NAME.fullmatch(name) is None
                or not isinstance(value, str) or value_size > _MAX_HEADER_VALUE
                or any(ord(character) < 32 or ord(character) == 127 for character in value)
            ):
                raise _HttpError("invalid_request")
            total += len(name.encode("ascii")) + len(value.encode("utf-8")) + 4
        if total > _MAX_HEADER_BYTES:
            raise _HttpError("invalid_request")
    @staticmethod
    def _validate_target(target: str) -> str:
        try:
            encoded = target.encode("ascii")
        except UnicodeEncodeError:
            raise _HttpError("invalid_request") from None
        if (
            not encoded or len(encoded) > _MAX_TARGET_BYTES
            or not target.startswith("/") or target.startswith("//")
            or any(character in target for character in ("%", "\\", "#"))
            or target.count("?") > 1
            or any(ord(character) < 32 or ord(character) == 127 for character in target)
        ):
            raise _HttpError("invalid_request")
        path = target.split("?", 1)[0]
        if path != "/" and (
            "//" in path or path.endswith("/")
            or any(segment in {".", ".."} for segment in path.split("/"))
        ):
            raise _HttpError("invalid_request")
        return target
    @staticmethod
    def _header_map(headers: tuple[tuple[str, str], ...]) -> dict[str, tuple[str, ...]]:
        grouped: dict[str, list[str]] = {}
        for name, value in headers:
            grouped.setdefault(name.lower(), []).append(value)
        return {name: tuple(values) for name, values in grouped.items()}
    def _validate_host(self, headers: Mapping[str, tuple[str, ...]]) -> None:
        values = headers.get("host", ())
        if len(values) != 1 or values[0] != self._authority:
            raise _HttpError("invalid_request")
    def _validate_origin(self, headers: Mapping[str, tuple[str, ...]],
                         method: str) -> None:
        values = headers.get("origin", ())
        expected = f"http://{self._authority}"
        if method == "POST":
            if len(values) != 1 or values[0] != expected:
                raise _HttpError("forbidden_origin")
        elif values and (len(values) != 1 or values[0] != expected):
            raise _HttpError("forbidden_origin")
    @staticmethod
    def _validate_framing(headers: Mapping[str, tuple[str, ...]], body: bytes) -> None:
        if headers.get("transfer-encoding") or headers.get("content-encoding"):
            raise _HttpError("invalid_request")
        lengths = headers.get("content-length", ())
        if len(lengths) > 1 or (
            lengths and (not lengths[0].isdigit() or bool(lengths[0].strip("0")))
        ) or body:
            raise _HttpError("invalid_request")
    def _validate_bearer(self, headers: Mapping[str, tuple[str, ...]]) -> None:
        values = headers.get("authorization", ())
        if len(values) != 1:
            raise _HttpError("unauthorized")
        try:
            matches = secrets.compare_digest(values[0], f"Bearer {self._token}")
        except TypeError:
            matches = False
        if not matches:
            raise _HttpError("unauthorized")
    @staticmethod
    def _parse_target(target: str) -> tuple[str, str | None]:
        if "?" not in target:
            return target, None
        path, query = target.split("?", 1)
        if not query:
            raise _HttpError("invalid_request")
        return path, query
    @staticmethod
    def _parse_query_fields(raw: str | None, *, allowed: tuple[str, ...],
                            required: tuple[str, ...] = ()) -> dict[str, str]:
        if raw is None:
            fields: dict[str, str] = {}
        else:
            fields = {}
            for item in raw.split("&"):
                if not item or item.count("=") != 1:
                    raise _HttpError("invalid_request")
                name, value = item.split("=", 1)
                if name not in allowed or name in fields or not value:
                    raise _HttpError("invalid_request")
                fields[name] = value
        if any(name not in fields for name in required):
            raise _HttpError("invalid_request")
        return fields
    @staticmethod
    def _reference(value: str | None, pattern: re.Pattern[str]) -> str | None:
        if value is None:
            return None
        if pattern.fullmatch(value) is None:
            raise _HttpError("invalid_request")
        return value
    def _current_refresh(self) -> object:
        current = self._controller.current()
        return (DashboardRefreshView(None, "idle", None, None, None, {}, ())
                if current is None else current.to_view())
    def _dispatch(self, method: str, path: str,
                  raw_query: str | None) -> DashboardResponse:
        if path in self._assets:
            if raw_query is not None:
                raise _HttpError("invalid_request")
            if method not in {"GET", "HEAD"}:
                raise _HttpError("method_not_allowed")
            asset = self._assets[path]
            return self._response(200, asset.content_type, asset.body, method)
        if not path.startswith("/api/v1/"):
            raise _HttpError("not_found")
        if path == "/api/v1/snapshot":
            values = self._parse_query_fields(raw_query, allowed=("project", "task"))
            project = self._reference(values.get("project"), _PROJECT_REF)
            task = self._reference(values.get("task"), _TASK_REF)
            if task is not None and project is None:
                raise _HttpError("invalid_request")
            if method not in {"GET", "HEAD"}:
                raise _HttpError("method_not_allowed")
            if live_snapshot_available(self): payload = _as_dict(self._query.snapshot(project_ref=project, task_ref=task, refresh=self._current_refresh()))
            elif task is not None:
                payload = _as_dict(self._query.snapshot(
                    project_ref=project, task_ref=task,
                    refresh=self._current_refresh(),
                ))
            else:
                selected = project
                if selected is None:
                    refs = self._cache.refs()
                    selected = refs[0] if refs else None
                if selected is None:
                    if self._fallback_error is not None and not self._controller.succeeded_once(): raise _HttpError(self._fallback_error)
                    if self._fallback_snapshot is None:
                        raise _HttpError("refresh_unavailable")
                    payload = _as_dict(self._fallback_snapshot)
                    payload["refresh"] = _as_dict(self._current_refresh())
                    return self._json(200, dashboard_payload(self, payload), method)
                cached = self._controller.snapshot(selected)
                if cached is None:
                    raise KeyError("unknown public reference")
                payload = _as_dict(cached)
            return self._json(200, dashboard_payload(self, payload), method)
        if path == "/api/v1/tasks":
            values = self._parse_query_fields(
                raw_query, allowed=("project", "cursor", "limit"),
                required=("project",),
            )
            project = self._reference(values["project"], _PROJECT_REF)
            cursor = self._reference(values.get("cursor"), _TASK_REF)
            limit_text = values.get("limit")
            if limit_text is not None and not limit_text.isdigit():
                raise _HttpError("invalid_request")
            limit = 50 if limit_text is None else int(limit_text)
            if not 1 <= limit <= 100:
                raise _HttpError("invalid_request")
            if method not in {"GET", "HEAD"}:
                raise _HttpError("method_not_allowed")
            return self._json(200, _as_dict(self._query.tasks(
                project, cursor=cursor, limit=limit,
            )), method)
        if path == "/api/v1/compare":
            values = self._parse_query_fields(
                raw_query, allowed=("project", "left", "right"),
                required=("project", "left", "right"),
            )
            project = self._reference(values["project"], _PROJECT_REF)
            left = self._reference(values["left"], _TASK_REF)
            right = self._reference(values["right"], _TASK_REF)
            if method not in {"GET", "HEAD"}:
                raise _HttpError("method_not_allowed")
            return self._json(
                200, _as_dict(self._query.compare(project, left, right)), method,
            )
        evidence_prefix = "/api/v1/evidence/"
        if path.startswith(evidence_prefix):
            evidence = self._reference(path.removeprefix(evidence_prefix), _EVIDENCE_REF)
            values = self._parse_query_fields(
                raw_query, allowed=("project",), required=("project",),
            )
            project = self._reference(values["project"], _PROJECT_REF)
            if method not in {"GET", "HEAD"}:
                raise _HttpError("method_not_allowed")
            return self._json(
                200, _as_dict(self._query.evidence(project, evidence)), method,
            )
        sync_response = dispatch_sync(self, method, path, raw_query)
        if sync_response is not None:
            return sync_response
        if path == "/api/v1/refresh":
            self._parse_query_fields(raw_query, allowed=())
            if method != "POST":
                raise _HttpError("method_not_allowed")
            try:
                current, reused = self._controller.start()
            except Exception:
                raise _HttpError("refresh_unavailable") from None
            payload = {
                "schema_version": "hydra.dashboard-refresh/v1",
                **_as_dict(current),
                "reused": bool(reused),
            }
            refresh_ref = self._reference(payload.get("refresh_ref"), _REFRESH_REF)
            assert refresh_ref is not None
            return self._json(
                202, payload, method,
                extra=(("Location", f"/api/v1/refresh/{refresh_ref}"),),
            )
        refresh_prefix = "/api/v1/refresh/"
        if path.startswith(refresh_prefix):
            self._parse_query_fields(raw_query, allowed=())
            if method not in {"GET", "HEAD"}:
                raise _HttpError("method_not_allowed")
            refresh_ref = self._reference(path.removeprefix(refresh_prefix), _REFRESH_REF)
            return self._json(200, {
                "schema_version": "hydra.dashboard-refresh/v1",
                **_as_dict(self._controller.get(refresh_ref)),
            }, method)
        raise _HttpError("not_found")
    def _json(self, status: int, payload: object, method: str, *,
              extra: tuple[tuple[str, str], ...] = ()) -> DashboardResponse:
        return self._response(
            status, "application/json; charset=utf-8", _json_bytes(payload), method,
            extra=extra,
        )
    def _error(self, code: str, method: str) -> DashboardResponse:
        status = _ERROR_STATUS[code]
        extra = (("WWW-Authenticate", "Bearer"),) if code == "unauthorized" else ()
        return self._json(status, {
            "schema_version": "hydra.dashboard-error/v1",
            "error": {"code": code},
        }, method, extra=extra)
    @staticmethod
    def _response(status: int, content_type: str, body: bytes, method: str, *,
                  extra: tuple[tuple[str, str], ...] = ()) -> DashboardResponse:
        headers = (
            *_SECURITY_HEADERS,
            *extra,
            ("Content-Type", content_type),
            ("Content-Length", str(len(body))),
            ("Connection", "close"),
        )
        return DashboardResponse(status, headers, b"" if method == "HEAD" else body)
def create_dashboard_server(*, port: int,
                            application: DashboardApplication) -> ThreadingHTTPServer:
    """Bind IPv4 loopback first, then install an authority-bound application."""
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        raise ValueError("dashboard port must be between 0 and 65535")
    if not isinstance(application, DashboardApplication):
        raise TypeError("application must be DashboardApplication")
    holder: dict[str, DashboardApplication] = {}
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def setup(self) -> None:
            super().setup()
            self.request.settimeout(2.0)
        def log_message(self, _format: str, *args: Any) -> None:
            del args
        log_error = log_message
        def __getattr__(self, name: str) -> Any:
            if name.startswith("do_"):
                return self._dispatch_request
            raise AttributeError(name)
        def handle_expect_100(self) -> bool:
            return True
        def send_error(self, code: int, message: str | None = None,
                       explain: str | None = None) -> None:
            del code, message, explain
            method = "HEAD" if getattr(self, "command", None) == "HEAD" else "GET"
            response = holder["application"]._error("invalid_request", method)
            self._write(response)
        def _dispatch_request(self) -> None:
            raw_items = getattr(self.headers, "raw_items", None)
            headers = tuple(raw_items() if callable(raw_items) else self.headers.items())
            response = holder["application"].handle(DashboardRequest(
                self.command, self.path, headers, b"",
            ))
            self._write(response)
        def _write(self, response: DashboardResponse) -> None:
            self.close_connection = True
            if self.request_version == "HTTP/0.9":
                self.request_version = "HTTP/1.1"
            try:
                self.send_response_only(response.status)
                for name, value in response.headers:
                    self.send_header(name, value)
                self.end_headers()
                if response.body:
                    self.wfile.write(response.body)
            except OSError:
                return
        do_GET = do_HEAD = do_POST = do_OPTIONS = _dispatch_request
        do_PUT = do_PATCH = do_DELETE = do_TRACE = do_CONNECT = _dispatch_request
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    host, actual_port = server.server_address
    if host != "127.0.0.1" or not isinstance(actual_port, int) or actual_port <= 0:
        server.server_close()
        raise RuntimeError("dashboard server did not bind numeric IPv4 loopback")
    server.handle_error = lambda _request, _address: None  # type: ignore[method-assign]
    holder["application"] = application.bound_to(f"{host}:{actual_port}")
    server.daemon_threads = True
    server.block_on_close = True
    return server
