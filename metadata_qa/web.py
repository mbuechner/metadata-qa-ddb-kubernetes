"""Flask HTTP and Socket.IO transport adapters."""

from __future__ import annotations

import base64
import binascii
import hmac
import logging
from collections.abc import Callable
from typing import Any, TypeVar, cast

from flask import Flask, Response, jsonify, render_template, request
from flask_socketio import SocketIO, emit

from metadata_qa.config import Settings
from metadata_qa.kubernetes_gateway import KubernetesError
from metadata_qa.models import JobStatus
from metadata_qa.service import JobNotFoundError, JobService

LOGGER = logging.getLogger(__name__)
Handler = TypeVar("Handler", bound=Callable[..., object])
HttpResponse = Response | tuple[Response, int]


def _path(settings: Settings, path: str) -> str:
    if path == "/":
        return settings.url_prefix or "/"
    return f"{settings.url_prefix}{path}"


def _socket_event(socketio: SocketIO, event: str) -> Callable[[Handler], Handler]:
    """Add precise typing around Flask-SocketIO's dynamically typed decorator."""
    return cast(Callable[[Handler], Handler], socketio.on(event))


def _socket_error_handler(socketio: SocketIO) -> Callable[[Handler], Handler]:
    return cast(Callable[[Handler], Handler], socketio.on_error_default)


def _unauthorized(settings: Settings) -> Response:
    response = Response(
        "Unauthorized",
        status=401,
        headers={"WWW-Authenticate": f'Basic realm="{settings.httpauth_realm}"'},
    )
    response.headers["Cache-Control"] = "no-store"
    return response


def _request_is_authorized(settings: Settings) -> bool:
    if not settings.httpauth_enabled:
        return True
    header = request.headers.get("Authorization")
    if not header:
        return False
    try:
        scheme, token = header.split(" ", 1)
        decoded = base64.b64decode(token.strip(), validate=True).decode("utf-8")
    except (ValueError, binascii.Error, UnicodeDecodeError):
        return False
    if scheme.casefold() != "basic" or ":" not in decoded:
        return False
    username, password = decoded.split(":", 1)
    return hmac.compare_digest(username, settings.httpauth_username or "") and hmac.compare_digest(
        password, settings.httpauth_password or ""
    )


def register_http_routes(app: Flask, service: JobService, settings: Settings) -> None:
    @app.before_request
    def require_http_authentication() -> Response | None:
        # Liveness/readiness responses contain no sensitive data and must work with standard
        # Kubernetes probes, which do not inherit browser Basic Auth credentials.
        if request.path in {_path(settings, "/healthz"), _path(settings, "/readyz")}:
            return None
        return None if _request_is_authorized(settings) else _unauthorized(settings)

    @app.after_request
    def add_security_headers(response: Response) -> Response:
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; "
            "script-src 'self' https://cdn.jsdelivr.net https://cdn.socket.io; "
            "style-src 'self' https://cdn.jsdelivr.net; "
            "connect-src 'self' ws: wss:; "
            "img-src 'self' data:; object-src 'none'; base-uri 'self'; "
            "frame-ancestors 'none'; form-action 'self'",
        )
        if request.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.get(_path(settings, "/healthz"))
    def health() -> tuple[dict[str, str], int]:
        return {"status": "ok"}, 200

    @app.get(_path(settings, "/readyz"))
    def readiness() -> tuple[dict[str, str], int]:
        try:
            service.check_ready()
            return {"status": "ready"}, 200
        except KubernetesError as exc:
            LOGGER.warning(
                "Readiness check failed: operation=%s status=%s", exc.operation, exc.status
            )
            return {"status": "not-ready"}, 503

    @app.get(_path(settings, "/"), strict_slashes=False)
    def index() -> str:
        return render_template("index.html", application_base_path=settings.url_prefix)

    @app.get(_path(settings, "/api/jobs"))
    def list_jobs() -> HttpResponse:
        try:
            return jsonify(service.list_jobs())
        except KubernetesError as exc:
            return _kubernetes_error_response("listing jobs", exc)

    @app.get(_path(settings, "/api/jobs/<job_name>/logs"))
    def job_logs(job_name: str) -> HttpResponse:
        raw_tail_lines = request.args.get("tailLines")
        if raw_tail_lines is None:
            tail_lines = settings.default_log_tail_lines
        else:
            try:
                tail_lines = int(raw_tail_lines)
            except ValueError:
                return jsonify(error="tailLines must be an integer"), 400
        if not 1 <= tail_lines <= settings.max_log_tail_lines:
            return (
                jsonify(error=f"tailLines must be between 1 and {settings.max_log_tail_lines}"),
                400,
            )
        try:
            return jsonify(service.get_logs(job_name, tail_lines=tail_lines))
        except JobNotFoundError:
            return jsonify(error="Unknown job"), 404
        except KubernetesError as exc:
            return _kubernetes_error_response("reading job logs", exc)

    @app.delete(_path(settings, "/api/jobs/<job_name>"))
    def delete_job(job_name: str) -> HttpResponse:
        try:
            return jsonify(service.delete_job(job_name))
        except JobNotFoundError:
            return jsonify(error="Unknown job"), 404
        except KubernetesError as exc:
            return _kubernetes_error_response("deleting a job", exc)


def _kubernetes_error_response(operation: str, error: KubernetesError) -> tuple[Response, int]:
    LOGGER.error(
        "Kubernetes failure while %s: operation=%s status=%s",
        operation,
        error.operation,
        error.status,
    )
    return jsonify(error="Kubernetes service is temporarily unavailable"), 503


def register_socket_handlers(socketio: SocketIO, service: JobService, settings: Settings) -> None:
    @_socket_event(socketio, "start_job")
    def start_job() -> None:
        service.request_start()

    @_socket_event(socketio, "cancel_job")
    def cancel_job() -> None:
        service.cancel_current_job()

    @_socket_event(socketio, "connect")
    def connect() -> bool | None:
        if not _request_is_authorized(settings):
            return False
        socket_request = cast(Any, request)
        LOGGER.info("Socket.IO client connected", extra={"socket_id": socket_request.sid})
        emit(
            "status_update",
            {
                "message": f"{socket_request.sid} mit Server verbunden",
                "status": JobStatus.RUNNING.value if service.current_job else "Stopped",
            },
        )
        return None

    @_socket_event(socketio, "disconnect")
    def disconnect(*_args: object) -> None:
        socket_request = cast(Any, request)
        LOGGER.info("Socket.IO client disconnected", extra={"socket_id": socket_request.sid})

    @_socket_error_handler(socketio)
    def default_error_handler(error: BaseException) -> None:
        socket_request = cast(Any, request)
        event = getattr(socket_request, "event", None)
        event_name = event.get("message") if isinstance(event, dict) else None
        LOGGER.error(
            "Unhandled Socket.IO error for event %r",
            event_name,
            exc_info=(type(error), error, error.__traceback__),
        )
