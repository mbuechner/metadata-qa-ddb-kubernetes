"""metadata-qa-ddb-kubernetes

Flask + Flask-SocketIO Web-UI zum manuellen Starten/Stoppen eines Kubernetes CronJobs
als Job und zum Live-Streaming der Pod-Logs.

Kubernetes-Konfiguration:
- In-Cluster: config.load_incluster_config()
- Lokal: config.load_kube_config()

Wichtige Env Vars:
- NAMESPACE (default: ddbmetadata-qa)
- CRONJOB_NAME (default: ddbmetadata-qa)
- SECRET_KEY (optional)
- CORS_ALLOWED_ORIGINS (default: *)
- START_POD_TIMEOUT_SECONDS (default: 120)
- LOG_STREAM_REQUEST_TIMEOUT_SECONDS (default: 10)

Hinweis: Diese App hält einen kleinen globalen Zustand (aktueller Job / Log-Stream),
damit mehrere Clients konsistent informiert werden können.
"""

import time
import logging
import os
import threading
import re
import base64
import binascii
import hmac
from typing import Any, cast
from flask import Flask, Response, render_template, request, copy_current_request_context
from flask_socketio import SocketIO, emit
from kubernetes import client, config
from kubernetes.client import ApiException

# Initialize Flask app
app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY") or os.urandom(24).hex()

cors_allowed_origins_env = os.getenv("CORS_ALLOWED_ORIGINS", "*")
cors_allowed_origins = (
    "*" if cors_allowed_origins_env.strip() == "*" else [o.strip() for o in cors_allowed_origins_env.split(",") if o.strip()]
)
socketio = SocketIO(app, cors_allowed_origins=cors_allowed_origins, async_mode="gevent")

# Server Port
#
# - Für Container/Kubernetes ist 8080 üblich.
# - Lokal kann via PORT überschrieben werden.
PORT = int(os.getenv("PORT", "8080"))

# Optionale HTTP Basic Auth
#
# Wenn HTTPAUTH_USERNAME und HTTPAUTH_PASSWORD gesetzt sind, wird Auth für alle HTTP-Routen
# (inkl. Socket.IO Handshake) erzwungen. Ohne diese Env Vars läuft die App offen.
HTTPAUTH_USERNAME = os.getenv("HTTPAUTH_USERNAME")
HTTPAUTH_PASSWORD = os.getenv("HTTPAUTH_PASSWORD")
HTTPAUTH_REALM = os.getenv("HTTPAUTH_REALM", "metadata-qa")


def _httpauth_enabled() -> bool:
    return bool(HTTPAUTH_USERNAME and HTTPAUTH_PASSWORD)


def _unauthorized_response() -> Response:
    return Response(
        "Unauthorized",
        status=401,
        headers={"WWW-Authenticate": f'Basic realm="{HTTPAUTH_REALM}"'},
    )


def _is_request_authorized() -> bool:
    if not _httpauth_enabled():
        return True

    auth_header = request.headers.get("Authorization")
    if not auth_header:
        return False

    # Format: "Basic <base64(user:pass)>"
    try:
        scheme, token = auth_header.split(" ", 1)
    except ValueError:
        return False
    if scheme.lower() != "basic":
        return False

    try:
        decoded = base64.b64decode(token.strip()).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return False

    if ":" not in decoded:
        return False
    username, password = decoded.split(":", 1)

    return hmac.compare_digest(username, HTTPAUTH_USERNAME or "") and hmac.compare_digest(
        password, HTTPAUTH_PASSWORD or ""
    )


@app.before_request
def _require_httpauth():
    if not _httpauth_enabled():
        return None
    if _is_request_authorized():
        return None
    return _unauthorized_response()

# Kubernetes API Configuration
try:
    # Läuft die App im Cluster, ist In-Cluster-Auth der schnellste Weg.
    config.load_incluster_config()
except config.ConfigException:
    # Außerhalb des Clusters wird die lokale kubeconfig verwendet.
    config.load_kube_config()

api_client = client.ApiClient()
api_core = client.CoreV1Api()
api_batch = client.BatchV1Api()

# Globaler Log-Stream Zustand
#
# - log_stream_active: steuert, ob der Streaming-Thread weiterläuft
# - log_stream_thread: Thread-Handle (Debug/Inspection)
# - log_stream_lock: synchronisiert Zugriff auf obigen Zustand
log_stream_active = False
log_stream_thread = None
log_stream_lock = threading.Lock()

# Globaler Job-Zustand (ein Job „Slot“)
#
# job_name ist der vom UI reservierte/gestartete Jobname. Zusätzlich wird im Cluster
# immer der echte Zustand geprüft, falls die App neu startet oder mehrere Clients
# parallel zugreifen.
job_state_lock = threading.Lock()

namespace = os.getenv("NAMESPACE", "ddbmetadata-qa")
cronjob_name = os.getenv("CRONJOB_NAME", "ddbmetadata-qa")
job_name = None

START_POD_TIMEOUT_SECONDS = int(os.getenv("START_POD_TIMEOUT_SECONDS", "120"))
LOG_STREAM_REQUEST_TIMEOUT_SECONDS = int(os.getenv("LOG_STREAM_REQUEST_TIMEOUT_SECONDS", "10"))

_DNS_LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")


def _is_managed_job_name(name: str) -> bool:
    """True, wenn der Name wie ein Job aussieht, den dieses UI verwaltet.

Der Jobname wird aus `${CRONJOB_NAME}-{timestamp}` gebildet und als Kubernetes DNS label validiert.
"""
    if not name:
        return False
    if not _DNS_LABEL_RE.match(name):
        return False
    return name.startswith(f"{cronjob_name}-")


def _k8s_object_name(obj) -> str:
    """Gibt obj.name zurück oder "".

Die Kubernetes Python Client-Modelle markieren einige Felder optional; diese Helper-Funktion
verhindert None-Zugriffe und beruhigt statische Typchecker.
"""
    return getattr(obj, "name", None) or ""


def _job_name(job: client.V1Job) -> str:
    """Gibt den Job-Namen robust zurück ("" wenn unbekannt)."""
    return _k8s_object_name(getattr(job, "metadata", None))


def _job_status_to_string(job: client.V1Job) -> str:
    """Mappt Job-Status auf einen kompakten String für UI/API."""
    status = job.status
    if not status:
        return "Unknown"
    if status.active and status.active > 0:
        return "Running"
    if status.succeeded and status.succeeded > 0:
        return "Succeeded"
    if status.failed and status.failed > 0:
        return "Failed"
    return "Pending"


def _job_to_dict(job: client.V1Job) -> dict:
    """Konvertiert ein V1Job in ein serialisierbares Dict für die UI."""
    status = job.status
    start_time = getattr(status, "start_time", None) if status else None
    completion_time = getattr(status, "completion_time", None) if status else None
    metadata = getattr(job, "metadata", None)
    is_terminating = bool(getattr(metadata, "deletion_timestamp", None))
    return {
        "name": _job_name(job),
        "status": _job_status_to_string(job),
        "startTime": start_time.isoformat() if start_time else None,
        "completionTime": completion_time.isoformat() if completion_time else None,
        "isTerminating": is_terminating,
    }


def _is_job_completed(job: client.V1Job) -> bool:
    """True, wenn Job succeeded/failed ist (nach Statuszählwerten)."""
    status = job.status
    if not status:
        return False
    if status.succeeded and status.succeeded > 0:
        return True
    if status.failed and status.failed > 0:
        return True
    return False


def _read_job_or_none(name: str) -> client.V1Job | None:
    """Liest einen Job oder gibt None zurück, wenn er nicht (mehr) existiert (404)."""
    try:
        return cast(client.V1Job, api_batch.read_namespaced_job(name=name, namespace=namespace))
    except ApiException as e:
        if getattr(e, "status", None) == 404:
            return None
        raise


def _is_job_terminating(job: client.V1Job) -> bool:
    metadata = getattr(job, "metadata", None)
    return bool(getattr(metadata, "deletion_timestamp", None))


def _find_incomplete_managed_job() -> tuple[str, str, bool] | None:
    """Return (job_name, status_string, is_terminating) for a managed job that is not completed yet."""
    jobs = cast(list[client.V1Job], api_batch.list_namespaced_job(namespace=namespace).items or [])
    managed = [j for j in jobs if _is_managed_job_name(_job_name(j)) and not _is_job_completed(j)]
    if not managed:
        return None

    def sort_key(j: client.V1Job):
        st = j.status.start_time if j.status and j.status.start_time else None
        return st or 0

    managed.sort(key=sort_key, reverse=True)
    j = managed[0]
    metadata = getattr(j, "metadata", None)
    is_terminating = bool(getattr(metadata, "deletion_timestamp", None))
    return _job_name(j), _job_status_to_string(j), is_terminating


def _wait_for_job_termination(job_to_wait_for: str):
    """Wartet, bis ein Job terminiert/gelöscht ist.

Wird im Hintergrund aufgerufen, damit `cancel_job` sofort zurückkehren kann.
"""
    global job_name
    try:
        while True:
            try:
                j = cast(client.V1Job, api_batch.read_namespaced_job(name=job_to_wait_for, namespace=namespace))
                if _is_job_completed(j):
                    break
            except ApiException as e:
                # 404 means the Job resource is gone
                if getattr(e, "status", None) == 404:
                    break
                # transient API errors: keep trying for a short while
            time.sleep(2)
    finally:
        with job_state_lock:
            if job_name == job_to_wait_for:
                job_name = None
        socketio.emit('status_update', {'message': f'Job {job_to_wait_for} ist terminiert.', 'status': 'Canceled'})


@app.get("/api/jobs")
def list_jobs():
    """List jobs created from the managed CronJob (by name prefix)."""
    global job_name
    try:
        # Prefer the real cluster state over local in-memory state.
        active = _find_incomplete_managed_job()

        # If no active job exists in the cluster, clear a potentially stale in-memory job_name.
        if not active:
            with job_state_lock:
                current = job_name
            if current and _is_managed_job_name(current):
                j = _read_job_or_none(current)
                if j is None or _is_job_completed(j):
                    with job_state_lock:
                        if job_name == current:
                            job_name = None

        jobs = cast(list[client.V1Job], api_batch.list_namespaced_job(namespace=namespace).items or [])
        managed = [j for j in jobs if _is_managed_job_name(_job_name(j))]

        def sort_key(j: client.V1Job):
            st = j.status.start_time if j.status and j.status.start_time else None
            return st or 0

        managed.sort(key=sort_key, reverse=True)
        return {
            "namespace": namespace,
            "cronjobName": cronjob_name,
            "activeJob": active[0] if active else job_name,
            "activeJobStatus": active[1] if active else None,
            "activeJobTerminating": active[2] if active else None,
            "jobs": [_job_to_dict(j) for j in managed],
        }
    except ApiException as e:
        return {"error": str(e)}, 500


@app.get("/api/jobs/<job>/logs")
def job_logs(job: str):
    """Fetch logs for the first pod of a given job."""
    if not _is_managed_job_name(job):
        return {"error": "Unknown job"}, 404

    try:
        tail_lines = request.args.get("tailLines", default=None, type=int)
        pod_list = api_core.list_namespaced_pod(namespace=namespace, label_selector=f"job-name={job}").items
        if not pod_list:
            return {"job": job, "pod": None, "logs": "(no pod found for job)", "status": "Unknown"}

        pod = pod_list[0]
        pod_name = pod.metadata.name
        pod_status = pod.status.phase if pod.status else "Unknown"
        logs = api_core.read_namespaced_pod_log(
            name=pod_name,
            namespace=namespace,
            tail_lines=tail_lines,
            timestamps=True,
        )

        return {"job": job, "pod": pod_name, "status": pod_status, "logs": logs}
    except ApiException as e:
        return {"error": str(e)}, 500


@app.delete("/api/jobs/<job>")
def delete_job(job: str):
    """Delete a managed job.

Intended for deleting jobs from the UI.

- Only allows managed job names (prefix + DNS label).
- If the job is still running, deletion acts like a cancel (Kubernetes will terminate pods).
"""
    global job_name, log_stream_active
    if not _is_managed_job_name(job):
        return {"error": "Unknown job"}, 404

    try:
        j = _read_job_or_none(job)
        if j is None:
            # Already deleted.
            return {"deleted": True, "job": job}

        is_completed = _is_job_completed(j)
        is_terminating = _is_job_terminating(j)

        # For running jobs, deleting is effectively a cancel.
        # Block new starts until termination is confirmed.
        if not is_completed or is_terminating:
            with job_state_lock:
                job_name = job
            with log_stream_lock:
                log_stream_active = False

            t = threading.Thread(target=_wait_for_job_termination, args=(job,), daemon=True)
            t.start()

        api_batch.delete_namespaced_job(
            name=job,
            namespace=namespace,
            body=client.V1DeleteOptions(propagation_policy='Foreground' if not is_completed else 'Background'),
        )
        return {"deleted": True, "job": job}
    except ApiException as e:
        # Consider 404 as already deleted.
        if getattr(e, "status", None) == 404:
            return {"deleted": True, "job": job}
        return {"error": str(e)}, 500

@app.route('/')
def index():
    """Rendert die Web-UI."""
    return render_template('index.html')

@socketio.on('start_job')
def start_job():
    """Startet einen neuen Job aus dem CronJob-Template.

Flow:
- Prüft, ob im Cluster bereits ein passender, unvollständiger Job existiert.
- Reserviert den globalen Job-Slot (`job_name`) um parallele Starts zu verhindern.
- Liest CronJob.spec.jobTemplate.spec, erzeugt daraus ein Job-Manifest und erstellt den Job.
- Wartet kurz auf einen Pod und startet dann (optional) Live-Log-Streaming.
"""
    global job_name, log_stream_active, log_stream_thread
    try:
        # If there's already an incomplete managed Job in the cluster, do not start a new one.
        existing = _find_incomplete_managed_job()
        if existing:
            existing_name, existing_status, is_terminating = existing
            with job_state_lock:
                job_name = existing_name
            if is_terminating:
                emit('status_update', {'message': f'Job {existing_name} wird noch terminiert. Bitte warten.', 'status': 'Stopping'}, broadcast=True)
            else:
                emit('status_update', {'message': f'Job {existing_name} läuft bereits.', 'status': existing_status}, broadcast=True)
            return

        # Kein aktiver Job im Cluster gefunden: räume ggf. stale in-memory State auf.
        # (z.B. wenn der Job extern gelöscht/abgebrochen wurde).
        with job_state_lock:
            current = job_name
        if current and _is_managed_job_name(current):
            try:
                j = _read_job_or_none(current)
            except ApiException as e:
                emit('status_update', {'message': f"API-Fehler beim Prüfen von Job {current}: {str(e)}", 'status': 'Error'}, broadcast=True)
                return

            if j is None or (j and _is_job_completed(j)):
                with job_state_lock:
                    if job_name == current:
                        job_name = None
            else:
                # Job existiert noch (oder wird terminiert) → Verhalten wie bisher.
                status = _job_status_to_string(j)
                if _is_job_terminating(j):
                    emit('status_update', {'message': f'Job {current} wird noch terminiert. Bitte warten.', 'status': 'Stopping'}, broadcast=True)
                else:
                    emit('status_update', {'message': f'Job {current} läuft bereits.', 'status': status}, broadcast=True)
                return

        with job_state_lock:
            if job_name:
                emit('status_update', {'message': f'Job {job_name} läuft bereits.', 'status': 'Running'}, broadcast=True)
                return

        # Fetch the CronJob template
        cronjob = cast(client.V1CronJob, api_batch.read_namespaced_cron_job(name=cronjob_name, namespace=namespace))

        # Define a unique job name
        this_job_name = f"{cronjob_name}-{int(time.time())}"

        # Reserve the job slot early so concurrent clients cannot start another one
        with job_state_lock:
            if job_name:
                emit('status_update', {'message': f'Job {job_name} läuft bereits.', 'status': 'Running'}, broadcast=True)
                return
            job_name = this_job_name

        # Create a new Job spec based on the CronJob
        cronjob_spec = getattr(cronjob, "spec", None)
        job_template = getattr(cronjob_spec, "job_template", None) if cronjob_spec else None
        job_template_spec = getattr(job_template, "spec", None) if job_template else None
        if not job_template_spec:
            emit(
                'status_update',
                {'message': f'CronJob {cronjob_name} hat kein Job-Template-Spec. Kann keinen Job erzeugen.', 'status': 'Error'},
                broadcast=True,
            )
            with job_state_lock:
                if job_name == this_job_name:
                    job_name = None
            return

        job_spec = api_client.sanitize_for_serialization(job_template_spec)
        if isinstance(job_spec, dict):
            # Ensure restartPolicy is set at PodTemplate level (spec.template.spec.restartPolicy)
            template = job_spec.setdefault("template", {})
            template_spec = template.setdefault("spec", {})
            template_spec.setdefault("restartPolicy", "Never")

        job_body = {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {
                "name": job_name,
                "namespace": namespace,
            },
            "spec": job_spec,
        }

        # Create the Job
        api_batch.create_namespaced_job(namespace=namespace, body=job_body)
        emit('status_update', {'message': f'Job {this_job_name} wird gestartet...', 'status': 'Starting'}, broadcast=True)

        last_pod_status = ''
        start_time = time.time()
        while True:
            # If the job was canceled/reset while we were waiting, stop cleanly.
            if job_name != this_job_name:
                break

            if time.time() - start_time > START_POD_TIMEOUT_SECONDS:
                emit('status_update', {'message': f'Kein Pod innerhalb von {START_POD_TIMEOUT_SECONDS}s für Job {this_job_name} gestartet.', 'status': 'Error'}, broadcast=True)
                with job_state_lock:
                    if job_name == this_job_name:
                        job_name = None
                break

            pod_list = api_core.list_namespaced_pod(namespace=namespace, label_selector=f"job-name={this_job_name}").items
            if pod_list:
                pod_status = pod_list[0].status.phase
                if last_pod_status != pod_status:
                    emit('status_update', {'message': f'Job-Status: {pod_status}', 'status': pod_status}, broadcast=True)
                    last_pod_status = pod_status
                with log_stream_lock:
                    if pod_status == "Running":
                        if not log_stream_active:
                            @copy_current_request_context
                            def thread_function():
                                broadcast_logs(app.app_context(), this_job_name)

                            log_stream_active = True
                            log_stream_thread = threading.Thread(target=thread_function)
                            log_stream_thread.start()
                        break
                    elif pod_status == "Failed":
                        emit('status_update', {'message': "Job konnte nicht gestartet werden", 'status': 'Error'}, broadcast=True)
                        with job_state_lock:
                            if job_name == this_job_name:
                                job_name = None
                        break
            time.sleep(1)

    except ApiException as e:
        error_body = e.body
        error_reason = e.reason
        emit('status_update', {'message': f"API-Fehler: {error_reason} - {error_body}", 'status': 'Error'}, broadcast=True)
        with job_state_lock:
            job_name = None

@socketio.on('cancel_job')
def cancel_job():
    """Bricht den aktuellen Job ab.

Vorgehen:
- Stoppt zuerst das Log-Streaming.
- Löscht den Job mit Foreground-Propagation.
- Wartet im Hintergrund bis der Job tatsächlich weg ist, um Doppelstarts zu verhindern.
"""
    global job_name, log_stream_active
    try:
        with job_state_lock:
            current_job = job_name

        if current_job:
            socketio.emit('status_update', {'message': f'Job {current_job} wird abgebrochen...', 'status': 'Stopping'})

            # Stop log streaming first (in case log stream is blocked waiting for new lines)
            with log_stream_lock:
                log_stream_active = False

            # Delete the job
            api_batch.delete_namespaced_job(
                name=current_job,
                namespace=namespace,
                body=client.V1DeleteOptions(propagation_policy='Foreground')
            )

            # Wait in background until the Job is actually gone/completed before allowing a new start.
            t = threading.Thread(target=_wait_for_job_termination, args=(current_job,), daemon=True)
            t.start()
        else:
            emit('status_update', {'message': 'Kein aktiver Job zum Abbrechen gefunden.', 'status': 'Idle'}, broadcast=True)

    except ApiException as e:
        emit('status_update', {'message': f"Fehler: {str(e)}", 'status': 'Error'}, broadcast=True)

def broadcast_logs(context, job_name_to_stream: str):
    """Streamt Logs des ersten Pods eines Jobs an alle verbundenen Clients."""
    global job_name, log_stream_active
    with context:
        try:
            while log_stream_active:
                # Check if the job exists
                pod_list = api_core.list_namespaced_pod(namespace=namespace, label_selector=f"job-name={job_name_to_stream}").items
                if not pod_list:
                    log_stream_active = False
                    with job_state_lock:
                        if job_name == job_name_to_stream:
                            job_name = None
                    break

                pod_name = pod_list[0].metadata.name
                pod_status = pod_list[0].status.phase

                if pod_status in ["Pending", "Running"]:
                    try:
                        # Stream logs directly from the Kubernetes API
                        log_stream = api_core.read_namespaced_pod_log(
                            name=pod_name,
                            namespace=namespace,
                            follow=True,
                            _preload_content=False,
                            _request_timeout=LOG_STREAM_REQUEST_TIMEOUT_SECONDS,
                        ).stream()

                        for line in log_stream:
                            if not log_stream_active:
                                return
                            emit('log_update', {'message': line.decode('utf-8').strip()}, broadcast=True)
                    except ApiException as e:
                        emit('status_update', {'message': f"{str(e)}", 'status': 'Error'}, broadcast=True)
                    except Exception as e:
                        # Covers read timeouts / connection resets during streaming
                        if not log_stream_active:
                            return
                        emit('status_update', {'message': f"{str(e)}", 'status': 'Error'}, broadcast=True)

                # Check if the pod is terminated
                if pod_status in ["Failed", "Succeeded"]:
                    emit('status_update', {'message': f'Pod {pod_name} Status: {pod_status}', 'status': pod_status}, broadcast=True)
                    log_stream_active = False
                    with job_state_lock:
                        if job_name == job_name_to_stream:
                            job_name = None
                    break

                time.sleep(3)

        except ApiException as e:
            emit('log_update', {'message': f"Fehler: {str(e)}"}, broadcast=True)
            log_stream_active = False
            with job_state_lock:
                if job_name == job_name_to_stream:
                    job_name = None
        except Exception as e:
            emit('log_update', {'message': f"Fehler: {str(e)}"}, broadcast=True)
            log_stream_active = False
            with job_state_lock:
                if job_name == job_name_to_stream:
                    job_name = None

@socketio.on('connect')
def connect():
    """Socket.IO connect handler (sendet initialen Status)."""
    # Zusätzliche Absicherung: je nach Transport/Upgrade-Pfad wird before_request
    # nicht in allen Fällen vor dem connect-Event ausgewertet.
    if _httpauth_enabled() and not _is_request_authorized():
        return False
    req = cast(Any, request)
    logging.info(f'{req.sid} connected')
    emit('status_update', {'message': f'{req.sid} mit Server verbunden', 'status': 'Running' if log_stream_active else 'Stopped'}, broadcast=False)

@socketio.on('disconnect')
def disconnect(*args):
    """Socket.IO disconnect handler.

Flask-SocketIO kann je nach Transport/Version einen Disconnect-Reason übergeben.
Darum akzeptieren wir optionale Args.
"""
    req = cast(Any, request)
    logging.info(f'{req.sid} disconnected')

@socketio.on_error_default
def default_error_handler(e):
    """Globaler Socket.IO Error Handler.

Flask-SocketIO ergänzt request um `event` (dict mit message/args). Das ist zur Laufzeit vorhanden,
aber nicht im Flask-Typmodell, daher der Any-cast.
"""
    req = cast(Any, request)
    event = getattr(req, "event", None) or {}
    if isinstance(event, dict):
        message = event.get("message")
        args = event.get("args")
    else:
        message = None
        args = None
    logging.error(f'SocketIO Error: {message} ({args})')
    logging.exception(e)


if __name__ == "__main__":
    # Dev/Local run. In Docker/Kubernetes wird i.d.R. gunicorn genutzt.
    socketio.run(app, host="0.0.0.0", port=PORT)
