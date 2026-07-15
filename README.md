# metadata-qa-ddb-kubernetes

Weboberfläche zum manuellen Starten, Abbrechen und Löschen eines Kubernetes-CronJobs sowie zum
Anzeigen historischer und live gestreamter Pod-Logs. Die Anwendung verwendet Flask,
Flask-SocketIO und den offiziellen Kubernetes-Python-Client.

## Funktionsweise

Die Anwendung liest das Job-Template des konfigurierten CronJobs, erzeugt daraus einen Job und
kennzeichnet ihn mit Management-Labels. Der Kubernetes-Cluster ist die maßgebliche Quelle für
Jobstatus und Wiederanlauf nach einem Prozessneustart. Prozesslokaler Zustand koordiniert nur einen
gerade startenden Job, dessen Abbruch und den zugehörigen Live-Log-Stream.

Die bestehenden Schnittstellen bleiben erhalten:

- HTTP: `GET /`, `GET /api/jobs`, `GET /api/jobs/<job>/logs`,
  `DELETE /api/jobs/<job>`
- Socket.IO Client → Server: `start_job`, `cancel_job`
- Socket.IO Server → Client: `status_update`, `log_update`

Zusätzlich stehen `GET /healthz` (Prozess lebt) und `GET /readyz` (CronJob ist über die
Kubernetes-API erreichbar) zur Verfügung.

Mit `URL_PREFIX=/app/manager` liegen alle genannten Pfade einschließlich Assets und Socket.IO
geschlossen unter `/app/manager`. Beispiele sind dann `/app/manager/api/jobs`,
`/app/manager/healthz` und `/app/manager/socket.io`.

## Architektur

```text
Browser (HTTP + Socket.IO)
          │
          ▼
metadata_qa.web             Transport, Authentifizierung, Eingabegrenzen
          │
          ▼
metadata_qa.service         Job-Lebenszyklus, Zustandskoordination, Events
          │
          ▼
metadata_qa.kubernetes_gateway
                            Timeouts, Clientmodelle, Labels, Ressourcenfreigabe
          │
          ▼
Kubernetes API              CronJob, Jobs, Pods und Pod-Logs
```

- `app.py`: rückwärtskompatibler WSGI- und lokaler Einstiegspunkt
- `metadata_qa/config.py`: unveränderliche und validierte Konfiguration
- `metadata_qa/models.py`: transport- und infrastrukturfremde Datenmodelle
- `metadata_qa/kubernetes_gateway.py`: einzige Grenze zum Kubernetes-Client
- `metadata_qa/service.py`: fachlicher Job-Lebenszyklus und nebenläufiger Zustand
- `metadata_qa/web.py`: Flask-/Socket.IO-Adapter und Sicherheitsheader
- `metadata_qa/application.py`: Application Factory und Zusammensetzung der Abhängigkeiten
- `static/app.js`: Browserlogik mit begrenztem Live-Log-Puffer

Die Architektur bleibt bewusst ein einzelner Dienst ohne DI-Framework oder zusätzlichen
Datenspeicher.

## Voraussetzungen und Installation

- Python 3.13 oder 3.14 für lokale Entwicklung
- Zugriff auf den Zielcluster über In-Cluster-Konfiguration oder lokale kubeconfig
- Ein vorhandener CronJob im konfigurierten Namespace

PowerShell:

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt
.venv\Scripts\python app.py
```

Bash:

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python app.py
```

Die UI läuft standardmäßig auf <http://localhost:8080>. Außerhalb eines Clusters wird die
Standard-kubeconfig verwendet. Kubernetes-Clients werden erst beim ersten Clusterzugriff erzeugt;
dadurch funktionieren Import, Liveness und isolierte Tests auch ohne kubeconfig.

Die Anwendung selbst lauscht im Container auf HTTP. HTTPS kann am Ingress oder Reverse Proxy
terminiert werden. Browser-API-Aufrufe und Socket.IO verwenden relative URLs und übernehmen damit
automatisch das Schema der geöffneten Seite (`http`/`ws` oder `https`/`wss`). Der separate
Kubernetes-API-Zugriff verwendet in-cluster weiterhin das von Kubernetes vorgegebene HTTPS mit
aktivierter Zertifikatsprüfung.

## Konfiguration

| Variable | Standard | Bedeutung und Grenzen |
|---|---:|---|
| `NAMESPACE` | `ddbmetadata-qa` | Ziel-Namespace; gültiges DNS-Label |
| `CRONJOB_NAME` | `ddbmetadata-qa` | Verwalteter CronJob; DNS-Label mit höchstens 52 Zeichen |
| `SECRET_KEY` | zufällig pro Start | Für Produktion als Secret mit stabilem, zufälligem Wert setzen |
| `CORS_ALLOWED_ORIGINS` | Same-Origin | `*` oder kommaseparierte Origins; `*` nur bewusst verwenden |
| `URL_PREFIX` | `/` | Optionaler Basispfad, beispielsweise `/app/manager` |
| `LOG_LEVEL` | `INFO` | `CRITICAL`, `ERROR`, `WARNING`, `INFO` oder `DEBUG` |
| `PORT` | `8080` | Lokaler Listen-Port, `1..65535` |
| `HTTPAUTH_USERNAME` | nicht gesetzt | Optionale Basic-Auth; nur gemeinsam mit Passwort zulässig |
| `HTTPAUTH_PASSWORD` | nicht gesetzt | Optionale Basic-Auth; nur gemeinsam mit Benutzer zulässig |
| `HTTPAUTH_REALM` | `metadata-qa` | Realm ohne Anführungs-/Steuerzeichen |
| `START_POD_TIMEOUT_SECONDS` | `120` | Maximale UI-Wartezeit auf einen Pod, höchstens 3600 s |
| `LOG_STREAM_REQUEST_TIMEOUT_SECONDS` | `10` | Timeout eines einzelnen Follow-Requests, höchstens 600 s |
| `KUBERNETES_REQUEST_TIMEOUT_SECONDS` | `10` | Timeout regulärer Kubernetes-Aufrufe, höchstens 600 s |
| `JOB_TERMINATION_TIMEOUT_SECONDS` | `120` | Maximale Hintergrundüberwachung eines Abbruchs, höchstens 3600 s |
| `DEFAULT_LOG_TAIL_LINES` | `2000` | Standardmenge historischer Logzeilen |
| `MAX_LOG_TAIL_LINES` | `5000` | Obergrenze je historischem Logabruf, höchstens 100000 |

Ungültige oder mehrdeutige Werte führen beim Start zu einer konkreten Fehlermeldung. Wenn Basic
Auth aktiviert ist, gilt sie für UI, API und Socket.IO. Liveness und Readiness bleiben für
Kubernetes-Probes absichtlich ohne Anmeldung erreichbar und geben keine vertraulichen Daten aus.
TLS sollte am Ingress oder Reverse Proxy terminiert werden; Basic Auth allein verschlüsselt keine
Zugangsdaten.

`URL_PREFIX` muss `/`, leer oder ein absoluter Pfad ohne abschließenden Slash sein. Der Default `/`
und ein leerer Wert bedeuten beide den Root-Pfad. Ein abschließender Slash wird normalisiert.
Querystrings, Prozentkodierung, `..`, Backslashes und leere
Pfadsegmente werden abgelehnt. Das Frontend erhält den normalisierten Wert vom Server und verwendet
ihn für API-, Asset- und Socket.IO-Aufrufe; es sind keine hartcodierten Root-URLs erforderlich.

## Docker

```bash
docker build -t metadata-qa-ddb-kubernetes:local .
docker run --rm -p 8080:8080 \
  -e NAMESPACE=ddbmetadata-qa \
  -e CRONJOB_NAME=ddbmetadata-qa \
  -v "$HOME/.kube/config:/app/.kube/config:ro" \
  -e KUBECONFIG=/app/.kube/config \
  metadata-qa-ddb-kubernetes:local
```

Das Image läuft als nicht privilegierter Benutzer, kopiert nur Laufzeitdateien und besitzt einen
Liveness-Healthcheck. Gunicorn verwendet absichtlich genau einen Worker.

## Kubernetes-Deployment

[k8s/rbac.yaml](k8s/rbac.yaml) enthält Namespace-begrenzte Minimalrechte.
[k8s/deployment.yaml](k8s/deployment.yaml) enthält ein gehärtetes Beispiel für Deployment und
Service mit Probes, Ressourcenlimits, `readOnlyRootFilesystem` und genau einer Replik.

Wenn `URL_PREFIX` im Kubernetes-Deployment geändert wird, müssen `livenessProbe.httpGet.path` und
`readinessProbe.httpGet.path` denselben Prefix erhalten. Der Docker-Healthcheck liest `URL_PREFIX`
automatisch aus der Umgebung.

Vor dem Ausrollen Image, Namespace, CronJob-Namen und gegebenenfalls das Secret
`metadata-qa-ddb-kubernetes` anpassen. Das Secret kann die Schlüssel `secret-key`, `username` und
`password` enthalten. Zugangsdaten sollten über das vorhandene Secret-Management des Clusters und
nicht als Klartextmanifest verwaltet werden.

```bash
kubectl apply -f k8s/rbac.yaml
kubectl apply -f k8s/deployment.yaml
kubectl -n ddbmetadata-qa rollout status deployment/metadata-qa-ddb-kubernetes
```

RBAC prüfen:

```bash
kubectl -n ddbmetadata-qa auth can-i create jobs.batch \
  --as=system:serviceaccount:ddbmetadata-qa:ddbmetadata-qa-sa
kubectl -n ddbmetadata-qa auth can-i get pods/log \
  --as=system:serviceaccount:ddbmetadata-qa:ddbmetadata-qa-sa
```

## OpenShift-Deployment

[k8s/openshift.yaml](k8s/openshift.yaml) enthält ein vollständiges Beispiel für OpenShift mit
ServiceAccount, RBAC, ConfigMap, Secret, Deployment, Service und Route. Es setzt keine feste UID und
ist damit für die übliche `restricted-v2` SCC ausgelegt. Die Edge-Route terminiert TLS am Router und
spricht den Container intern weiterhin über HTTP an. Mit `insecureEdgeTerminationPolicy: Allow`
sind beispielhaft HTTP und HTTPS erreichbar; mit `Redirect` kann HTTP auf HTTPS umgeleitet werden.
Das Beispiel verwendet standardmäßig `URL_PREFIX=/`, Probe-Pfade unter `/` und eine Route mit
`spec.path: /`. Für eine Bereitstellung unter `/app/manager` müssen `URL_PREFIX`, beide Probe-Pfade
und `spec.path` gemeinsam auf diesen Präfix umgestellt werden.

Vor dem Anwenden mindestens Image, CronJob-Namen, `SECRET_KEY` und Basic-Auth-Zugangsdaten ändern:

```bash
oc project ddbmetadata-qa
oc apply -f k8s/openshift.yaml
oc rollout status deployment/metadata-qa-ddb-kubernetes
oc get route metadata-qa-ddb-kubernetes
```

Für private Images muss zusätzlich ein Image-Pull-Secret am ServiceAccount hinterlegt werden.

## API

### `GET /api/jobs`

Liefert alle verwalteten Jobs sowie `activeJob`, `activeJobStatus` und
`activeJobTerminating`. Pro Request wird die Jobliste einmal aus Kubernetes gelesen.

### `GET /api/jobs/<job>/logs?tailLines=200`

Liefert historische Logs eines passenden Pods. `tailLines` muss zwischen `1` und
`MAX_LOG_TAIL_LINES` liegen. Ohne Parameter gilt `DEFAULT_LOG_TAIL_LINES`.

### `DELETE /api/jobs/<job>`

Löscht einen verwalteten Job idempotent. Aktive Jobs werden mit Foreground-Propagation terminiert;
abgeschlossene Jobs werden im Hintergrund gelöscht.

Fehlerdetails des Kubernetes-Clients werden serverseitig protokolliert, aber nicht an Browser oder
API-Aufrufer weitergereicht.

## Entwicklung und Qualitätssicherung

```bash
python -m pytest --cov --cov-report=term-missing
python -m ruff check .
python -m ruff format --check .
python -m mypy
python -m pip check
python -m pip_audit -r requirements.txt
```

Die zentrale Konfiguration steht in `pyproject.toml`. Tests verwenden Fake-Gateways und rufen
keinen echten Cluster auf. CI führt Linting, Formatprüfung, Typprüfung, Abhängigkeitsaudit und Tests
vor dem Container-Build aus.

## Sicherheit und Jobzuordnung

Neue Jobs erhalten die Labels:

- `app.kubernetes.io/managed-by=metadata-qa-ddb-kubernetes`
- `metadata-qa-ddb-kubernetes/cronjob=<CRONJOB_NAME>`

Historische Jobs ohne Labels bleiben übergangsweise sichtbar und löschbar, wenn ihr Name exakt dem
alten Muster `<CRONJOB_NAME>-<10 bis 13 Ziffern>` entspricht. Beliebige Präfixtreffer werden nicht
mehr als Eigentumsnachweis akzeptiert. Für eine vollständig labelbasierte Abgrenzung sollten alte
Jobs nach der Migration auslaufen oder administrativ bereinigt werden.

Browsermeldungen werden ausschließlich als Text gerendert. Sicherheitsheader einschließlich CSP,
Frame-Schutz und `nosniff` werden zentral gesetzt. Live-Logs sind im Browser auf 5000 DOM-Zeilen
begrenzt.

## Migration gegenüber älteren Versionen

- `CORS_ALLOWED_ORIGINS` ist ohne expliziten Wert jetzt Same-Origin statt `*`. Für bewusst
  benötigte Cross-Origin-Clients die erlaubten Origins explizit setzen.
- Nur eine gesetzte Basic-Auth-Variable ist jetzt ein Konfigurationsfehler statt still deaktivierter
  Authentifizierung.
- Historische Logs sind standardmäßig und maximal begrenzt; unzulässige `tailLines` liefern HTTP
  400.
- CronJob-Namen über 52 Zeichen werden früh abgelehnt.
- Kubernetes-Fehler werden als neutrales HTTP 503 gemeldet; Details stehen nur im Serverlog.
- Neue Jobs werden mit Management-Labels erzeugt; das enge Legacy-Namensschema bleibt kompatibel.
- Alle bisherigen HTTP-Routen, Socket.IO-Eventnamen und erfolgreichen Payload-Felder bleiben
  unverändert.

## Bekannte Einschränkungen

- Genau eine Replik und ein Gunicorn-Worker sind unterstützt. Horizontale Skalierung erfordert
  Sticky Sessions, einen Socket.IO-Message-Broker und verteilte Jobkoordination.
- Status- und Logereignisse werden weiterhin an alle verbundenen Clients verteilt.
- Bei mehreren Pods eines Jobs wird der laufende beziehungsweise neueste relevante Pod gewählt;
  Logs mehrerer paralleler Pods werden nicht zusammengeführt.
- Bootstrap/Bootswatch und der Socket.IO-Browserclient werden von externen CDNs geladen. In
  abgeschotteten Umgebungen sollten diese Assets intern gespiegelt werden.
- Die automatisierten Tests simulieren Kubernetes. Ein echter Cluster-Smoke-Test muss in der
  jeweiligen Zielumgebung erfolgen.

## Troubleshooting

- **`/readyz` liefert 503:** kubeconfig/In-Cluster-Konfiguration, CronJob-Name und RBAC prüfen.
- **`HTTPSConnectionPool(...:443): Read timed out`:** Diese Meldung betrifft die Kubernetes-API,
  nicht HTTPS der Weboberfläche. In-Cluster ignoriert die Anwendung für diesen clusterinternen
  Zugriff jetzt Umgebungs-Proxys, sodass ein unvollständiges `NO_PROXY` nicht mehr zum Timeout
  führt. Bleibt der Timeout bestehen, blockiert meist eine NetworkPolicy oder Cluster-Firewall den
  Zugriff des Pods auf den Kubernetes-Service.
- **Kein Pod erscheint:** CronJob-Template, Image-Pull und Scheduler-Events prüfen; gegebenenfalls
  `START_POD_TIMEOUT_SECONDS` erhöhen.
- **Live-Logs verbinden sich wiederholt:** Netzwerk/Ingress und
  `LOG_STREAM_REQUEST_TIMEOUT_SECONDS` prüfen. Ein Timeout wird automatisch erneut verbunden.
- **403 von Kubernetes:** ServiceAccount und die Regeln in `k8s/rbac.yaml` kontrollieren.
