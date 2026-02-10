# metadata-qa-ddb-kubernetes

Kleine Flask + Socket.IO Weboberfläche, um einen Kubernetes **CronJob** manuell als **Job** zu starten/abzubrechen und **Pod-Logs live** im Browser anzuzeigen.

Die App liest Kubernetes-Konfiguration automatisch aus:

- **In-Cluster:** `load_incluster_config()`
- **Lokal/außerhalb:** `load_kube_config()` (dein `kubeconfig`)

## Konfiguration (Env Vars)

- `NAMESPACE` (default: `ddbmetadata-qa`)
- `CRONJOB_NAME` (default: `ddbmetadata-qa`)
- `SECRET_KEY` (optional; wenn nicht gesetzt, wird ein zufälliger Key generiert)
- `CORS_ALLOWED_ORIGINS` (default: `*`; ansonsten Komma-separierte Liste, z.B. `https://example.com,https://intranet.local`)
- `START_POD_TIMEOUT_SECONDS` (default: `120`) – max. Wartezeit bis ein Pod zum Job erscheint
- `LOG_STREAM_REQUEST_TIMEOUT_SECONDS` (default: `10`) – Request-Timeout für Log-Streaming
- `PORT` (default: `8080`) – nur relevant für lokalen Start via `python app.py` (Docker/Gunicorn nutzt 8080 fest)

### Optional: HTTP Basic Auth

Wenn gesetzt, ist für **alle HTTP-Endpunkte inkl. Socket.IO** eine Authentifizierung nötig (Browser zeigt Login-Prompt).

- `HTTPAUTH_USERNAME`
- `HTTPAUTH_PASSWORD`
- `HTTPAUTH_REALM` (optional; default: `metadata-qa`)

## Smoke-Test (Docker)

### 1) Image bauen

```bash
docker build -t metadata-qa-ddb-kubernetes:local .
```

### 2) Container starten

Die App läuft auf Port `8080`.

**Variante A: Zugriff auf Cluster via lokalem kubeconfig**

```bash
docker run --rm -p 8080:8080 \
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
- http://localhost:8080
- "Start Job" startet einen neuen Job aus dem CronJob-Template.
- "Stop Job" bricht den aktuellen Job ab.

## Lokaler Run (ohne Docker)

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python app.py
```

Hinweis: Für produktiv empfiehlt sich Gunicorn (siehe Dockerfile).

## API

Die UI nutzt zusätzlich JSON-Endpunkte, die du auch direkt testen kannst.

### GET /api/jobs

Listet alle Jobs, die zum verwalteten CronJob gehören (Prefix: `${CRONJOB_NAME}-`).

Beispiel:

```bash
curl http://localhost:8080/api/jobs
```

Antwort (Beispiel-Felder):

- `activeJob`: aktuell laufender Jobname (oder `null`)
- `activeJobStatus`: Status-String (`Running`, `Succeeded`, `Failed`, ...)
- `jobs`: Liste mit `{ name, status, startTime, completionTime, isTerminating }`

### GET /api/jobs/<job>/logs

Holt Logs des ersten Pods, der zu einem Job gehört.

Optional:

- `tailLines`: letzte N Zeilen

Beispiel:

```bash
curl "http://localhost:8080/api/jobs/<job>/logs?tailLines=200"
```

## Socket.IO Events

Der Browser verbindet sich via Socket.IO und nutzt folgende Events:

- Client → Server: `start_job`
- Client → Server: `cancel_job`
- Server → Client: `status_update` (Payload: `{ message, status }`)
- Server → Client: `log_update` (Payload: `{ message }`)

Hinweis: `status_update` und `log_update` werden in der Regel an alle Clients gebroadcastet.

## Kubernetes RBAC (Minimalbedarf)

Der ServiceAccount braucht im Ziel-Namespace typischerweise Rechte für:

- `batch/cronjobs`: `get`
- `batch/jobs`: `create`, `get`, `list`, `delete`
- `pods`: `get`, `list`
- `pods/log`: `get`

(Die exakte Minimalmenge hängt an eurer Cluster-Policy/Labels ab.)

## Troubleshooting

- **403 Forbidden / RBAC:** ServiceAccount-Rechte prüfen (siehe RBAC-Abschnitt). Typische fehlende Rechte: `batch/jobs delete` oder `pods/log get`.
- **Kein Pod erscheint:** `START_POD_TIMEOUT_SECONDS` erhöhen oder CronJob-Template/Images prüfen.
- **Logs bleiben stehen:** `LOG_STREAM_REQUEST_TIMEOUT_SECONDS` erhöhen (bei sehr langsamen/clusternahen Verbindungen) oder Netzwerk/Ingress prüfen.
- **Docker + kubeconfig:** In den Beispielen wird `-v %USERPROFILE%\.kube\config:/root/.kube/config:ro` verwendet (Linux-Container). Wenn du mit einem anderen User im Container arbeitest, muss der Pfad im Container ggf. angepasst werden.

## Entwicklung / Checks

- Syntax-/Import-Check: `py -m py_compile app.py`
- Wenn VS Code „Fehler“ anzeigt, sind das oft Pylance/Typing-Diagnosen (statische Analyse) – nicht zwingend Runtime-Probleme.

## Hinweise

- Es kann immer nur **ein** Job gleichzeitig laufen (globaler Zustand).
- Status/Logs werden an alle verbundenen Clients gebroadcastet.
- Für produktive Nutzung unbedingt CORS einschränken und Auth davor schalten (z.B. OAuth2-Proxy/Ingress-Auth).