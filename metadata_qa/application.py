"""Application factory and dependency composition root."""

from __future__ import annotations

import logging
from pathlib import Path

from flask import Flask
from flask_socketio import SocketIO

from metadata_qa.config import Settings
from metadata_qa.kubernetes_gateway import JobGateway, KubernetesGateway
from metadata_qa.service import JobService
from metadata_qa.web import register_http_routes, register_socket_handlers

LOGGER = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def create_app(
    settings: Settings | None = None,
    gateway: JobGateway | None = None,
    *,
    async_mode: str = "gevent",
) -> Flask:
    """Create the web application; Kubernetes initialization remains lazy."""
    resolved_settings = settings or Settings.from_env()
    app = Flask(
        __name__,
        template_folder=str(PROJECT_ROOT / "templates"),
        static_folder=str(PROJECT_ROOT / "static"),
        static_url_path=f"{resolved_settings.url_prefix}/static",
    )
    app.config.update(
        APPLICATION_ROOT=resolved_settings.url_prefix or "/",
        SECRET_KEY=resolved_settings.secret_key,
        SESSION_COOKIE_PATH=resolved_settings.url_prefix or "/",
    )

    socketio_options: dict[str, object] = {
        "async_mode": async_mode,
        "path": f"{resolved_settings.url_prefix}/socket.io",
    }
    if resolved_settings.cors_allowed_origins is not None:
        socketio_options["cors_allowed_origins"] = resolved_settings.cors_allowed_origins
    socketio = SocketIO(app, **socketio_options)

    service = JobService(
        resolved_settings,
        gateway or KubernetesGateway(resolved_settings),
        sleep=socketio.sleep,
    )
    service.set_event_transport(
        publish=lambda event, payload: socketio.emit(event, payload),
        spawn=lambda target: socketio.start_background_task(target),
    )
    register_http_routes(app, service, resolved_settings)
    register_socket_handlers(socketio, service, resolved_settings)

    app.extensions["metadata_qa.socketio"] = socketio
    app.extensions["metadata_qa.service"] = service
    app.extensions["metadata_qa.settings"] = resolved_settings

    if resolved_settings.cors_allowed_origins == "*":
        LOGGER.warning("CORS_ALLOWED_ORIGINS=* permits cross-origin Socket.IO connections")
    if not resolved_settings.httpauth_enabled:
        LOGGER.warning("HTTP Basic Auth is disabled")
    if resolved_settings.secret_key_is_ephemeral:
        LOGGER.warning("SECRET_KEY is ephemeral")
    return app
