FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

RUN groupadd --system app && useradd --system --gid app --home-dir /app app

COPY --chown=app:app app.py ./
COPY --chown=app:app metadata_qa ./metadata_qa
COPY --chown=app:app static ./static
COPY --chown=app:app templates ./templates

USER app

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; prefix=os.getenv('URL_PREFIX', '/').rstrip('/'); urllib.request.urlopen(f'http://127.0.0.1:8080{prefix}/healthz', timeout=2)"]

# Multiple workers require sticky sessions and a shared Socket.IO message queue.
CMD ["gunicorn", "--workers", "1", "--bind", "0.0.0.0:8080", "--worker-class", "geventwebsocket.gunicorn.workers.GeventWebSocketWorker", "--access-logfile", "-", "--error-logfile", "-", "app:app"]
