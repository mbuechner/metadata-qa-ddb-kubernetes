import time
import logging
import os
import threading
import json
import re
from flask import Flask, render_template, request, copy_current_request_context
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
socketio = SocketIO(app, cors_allowed_origins=cors_allowed_origins)

# Kubernetes API Configuration
try:
    config.load_incluster_config()
except config.ConfigException:
    config.load_kube_config()

api_client = client.ApiClient()
api_core = client.CoreV1Api()
api_batch = client.BatchV1Api()

# Log stream
log_stream_active = False
log_stream_thread = None
log_stream_lock = threading.Lock()
job_state_lock = threading.Lock()

namespace = os.getenv("NAMESPACE", "ddbmetadata-qa")
cronjob_name = os.getenv("CRONJOB_NAME", "ddbmetadata-qa")
job_name = None

START_POD_TIMEOUT_SECONDS = int(os.getenv("START_POD_TIMEOUT_SECONDS", "120"))
LOG_STREAM_REQUEST_TIMEOUT_SECONDS = int(os.getenv("LOG_STREAM_REQUEST_TIMEOUT_SECONDS", "10"))

_DNS_LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")


def _is_managed_job_name(name: str) -> bool:
    if not name:
        return False
    if not _DNS_LABEL_RE.match(name):
        return False
    return name.startswith(f"{cronjob_name}-")


def _job_status_to_string(job: client.V1Job) -> str:
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
    status = job.status
    start_time = getattr(status, "start_time", None) if status else None
    completion_time = getattr(status, "completion_time", None) if status else None
    return {
        "name": job.metadata.name,
        "status": _job_status_to_string(job),
        "startTime": start_time.isoformat() if start_time else None,
        "completionTime": completion_time.isoformat() if completion_time else None,
    }


@app.get("/api/jobs")
def list_jobs():
    """List jobs created from the managed CronJob (by name prefix)."""
    try:
        jobs = api_batch.list_namespaced_job(namespace=namespace).items
        managed = [j for j in jobs if _is_managed_job_name(j.metadata.name)]

        def sort_key(j: client.V1Job):
            st = j.status.start_time if j.status and j.status.start_time else None
            return st or 0

        managed.sort(key=sort_key, reverse=True)
        return {
            "namespace": namespace,
            "cronjobName": cronjob_name,
            "activeJob": job_name,
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

@app.route('/')
def index():
    return render_template('index.html')

@socketio.on('start_job')
def start_job():
    global job_name, log_stream_active, log_stream_thread
    try:
        with job_state_lock:
            if job_name:
                emit('status_update', {'message': f'Job {job_name} is already running.', 'status': 'Running'}, broadcast=True)
                return

        # Fetch the CronJob template
        cronjob = api_batch.read_namespaced_cron_job(name=cronjob_name, namespace=namespace)

        # Define a unique job name
        this_job_name = f"{cronjob_name}-{int(time.time())}"

        # Reserve the job slot early so concurrent clients cannot start another one
        with job_state_lock:
            if job_name:
                emit('status_update', {'message': f'Job {job_name} is already running.', 'status': 'Running'}, broadcast=True)
                return
            job_name = this_job_name

        # Create a new Job spec based on the CronJob
        job_spec = api_client.sanitize_for_serialization(cronjob.spec.job_template.spec)
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
        emit('status_update', {'message': f'Job {this_job_name} is starting...', 'status': 'Starting'}, broadcast=True)

        last_pod_status = ''
        start_time = time.time()
        while True:
            # If the job was canceled/reset while we were waiting, stop cleanly.
            if job_name != this_job_name:
                break

            if time.time() - start_time > START_POD_TIMEOUT_SECONDS:
                emit('status_update', {'message': f'No pod started within {START_POD_TIMEOUT_SECONDS}s for job {this_job_name}.', 'status': 'Error'}, broadcast=True)
                with job_state_lock:
                    if job_name == this_job_name:
                        job_name = None
                break

            pod_list = api_core.list_namespaced_pod(namespace=namespace, label_selector=f"job-name={this_job_name}").items
            if pod_list:
                pod_status = pod_list[0].status.phase
                if last_pod_status != pod_status:
                    emit('status_update', {'message': f'Job status: {pod_status}', 'status': pod_status}, broadcast=True)
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
                        emit('status_update', {'message': f"Starting job failed", 'status': 'Error'}, broadcast=True)  
                        with job_state_lock:
                            if job_name == this_job_name:
                                job_name = None
                        break
            time.sleep(1)

    except ApiException as e:
        error_body = e.body
        error_reason = e.reason
        emit('status_update', {'message': f"API Error: {error_reason} - {error_body}", 'status': 'Error'}, broadcast=True)
        with job_state_lock:
            job_name = None

@socketio.on('cancel_job')
def cancel_job():
    global job_name, log_stream_active
    try:
        with job_state_lock:
            current_job = job_name

        if current_job:
            # Stop log streaming first (in case log stream is blocked waiting for new lines)
            with log_stream_lock:
                log_stream_active = False

            # Delete the job
            api_batch.delete_namespaced_job(
                name=current_job,
                namespace=namespace,
                body=client.V1DeleteOptions(propagation_policy='Foreground')
            )
            emit('status_update', {'message': f'Job {current_job} has been canceled.', 'status': 'Canceled'}, broadcast=True)
            with job_state_lock:
                if job_name == current_job:
                    job_name = None
        else:
            emit('status_update', {'message': 'No active job found to cancel.', 'status': 'Idle'}, broadcast=True)

    except ApiException as e:
        emit('status_update', {'message': f"Error: {str(e)}", 'status': 'Error'}, broadcast=True)

def broadcast_logs(context, job_name_to_stream: str):
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
                    emit('status_update', {'message': f'Pod {pod_name} status: {pod_status}', 'status': pod_status}, broadcast=True)
                    log_stream_active = False
                    job_name = None
                    break

                time.sleep(3)

        except ApiException as e:
            emit('log_update', {'message': f"Error: {str(e)}"}, broadcast=True)
            log_stream_active = False
            job_name = None
        except Exception as e:
            emit('log_update', {'message': f"Error: {str(e)}"}, broadcast=True)
            log_stream_active = False
            job_name = None

@socketio.on('connect')
def connect():
    logging.info(f'{request.sid} connected')
    emit('status_update', {'message': f'{request.sid} connected to server', 'status': 'Running' if log_stream_active else 'Stopped'}, broadcast=False)

@socketio.on('disconnect')
def disconnect(data):
    logging.info(f'{request.sid} disconnected')

@socketio.on_error_default
def default_error_handler(e):
    logging.error(f'SocketIO Error: {request.event["message"]} ({request.event["args"]})')
    logging.exception(e)
