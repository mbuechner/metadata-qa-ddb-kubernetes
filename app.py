"""Backward-compatible WSGI and local-development entry point."""

import logging

from metadata_qa import create_app
from metadata_qa.config import Settings

settings = Settings.from_env()
logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
app = create_app(settings)
socketio = app.extensions["metadata_qa.socketio"]


if __name__ == "__main__":
    # Listening on all interfaces is intentional for container/Kubernetes networking.
    socketio.run(app, host="0.0.0.0", port=settings.port)  # noqa: S104
