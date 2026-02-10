# metadata-qa-ddb-kubernetes

Kleine Flask + Socket.IO Weboberfläche, um einen Kubernetes **CronJob** manuell als **Job** zu starten/abzubrechen und **Pod-Logs live** im Browser anzuzeigen.

## Konfiguration (Env Vars)

- `NAMESPACE` (default: `ddbmetadata-qa`)
- `CRONJOB_NAME` (default: `ddbmetadata-qa`)
- `SECRET_KEY` (optional; wenn nicht gesetzt, wird ein zufälliger Key generiert)
- `CORS_ALLOWED_ORIGINS` (default: `*`; ansonsten Komma-separierte Liste, z.B. `https://example.com,https://intranet.local`)
- `START_POD_TIMEOUT_SECONDS` (default: `120`) – max. Wartezeit bis ein Pod zum Job erscheint
- `LOG_STREAM_REQUEST_TIMEOUT_SECONDS` (default: `10`) – Request-Timeout für Log-Streaming

## Smoke-Test (Docker)

### 1) Image bauen

```bash
docker build -t metadata-qa-ddb-kubernetes:local .
```

### 2) Container starten

Die App läuft auf Port `5000`.

**Variante A: Zugriff auf Cluster via lokalem kubeconfig**

```bash
docker run --rm -p 5000:5000 \
  -e NAMESPACE=ddbmetadata-qa \
  -e CRONJOB_NAME=ddbmetadata-qa \
  -v %USERPROFILE%\.kube\config:/root/.kube/config:ro \
  metadata-qa-ddb-kubernetes:local
```

**Variante B: In-Cluster (Kubernetes Deployment)**

Im Cluster nutzt die App automatisch `load_incluster_config()`.

Dann einfach die App wie üblich deployen (Deployment/Service/Ingress). Wichtig: ServiceAccount/RBAC muss passen (siehe unten).

### 3) Browser öffnen

- http://localhost:5000
- "Start Job" startet einen neuen Job aus dem CronJob-Template.
- "Stop Job" bricht den aktuellen Job ab.

## Lokaler Run (ohne Docker)

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python app.py
```

Hinweis: Für produktiv empfiehlt sich Gunicorn (siehe Dockerfile).

## Kubernetes RBAC (Minimalbedarf)

Der ServiceAccount braucht im Ziel-Namespace typischerweise Rechte für:

- `batch/cronjobs`: `get`
- `batch/jobs`: `create`, `get`, `list`, `delete`
- `pods`: `get`, `list`
- `pods/log`: `get`

(Die exakte Minimalmenge hängt an eurer Cluster-Policy/Labels ab.)

## Hinweise

- Es kann immer nur **ein** Job gleichzeitig laufen (globaler Zustand).
- Status/Logs werden an alle verbundenen Clients gebroadcastet.
- Für produktive Nutzung unbedingt CORS einschränken und Auth davor schalten (z.B. OAuth2-Proxy/Ingress-Auth).