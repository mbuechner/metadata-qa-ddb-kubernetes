import time
import logging
import os
import threading
from flask import Flask, render_template, request, copy_current_request_context
from flask_socketio import SocketIO, emit
from kubernetes import client, config

# Initialize Flask app
app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY")
socketio = SocketIO(app, cors_allowed_origins="*")

# Kubernetes API Configuration
try:
    config.load_incluster_config()
except config.ConfigException:
    config.load_kube_config()

v1 = client.CoreV1Api()
apps_v1 = client.AppsV1Api()
batch_v1 = client.BatchV1Api()

# Log stream
log_stream_active = False
log_stream_thread = None
log_stream_lock = threading.Lock()

namespace = "ddbmetadata-qa"
cronjob_name = "ddbmetadata-qa"
job_name = ""

@app.route('/')
def index():
    return render_template('index.html')

@socketio.on('start_job')
def start_job():
    global job_name, log_stream_active, log_stream_thread
    try:
        if job_name:
            emit('status_update', {'message': f'Job {job_name} is already running.', 'status': 'Running'}, broadcast=True)
            return

        # Fetch the CronJob template
        cronjob = batch_v1.read_namespaced_cron_job(name=cronjob_name, namespace=namespace)

        # Define a unique job name
        job_name = f"{cronjob_name}-{int(time.time())}"

        # Create a new Job spec based on the CronJob
        job_body = {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {
                "name": job_name,
                "namespace": namespace,
                "labels": f"job-name={job_name}"
            },
            "spec": cronjob.spec.job_template.spec
        }

        # Create the Job
        batch_v1.create_namespaced_job(namespace=namespace, body=job_body, label_selector=f"job-name={job_name}")
        emit('status_update', {'message': f'Job {job_name} is starting...', 'status': 'Starting'}, broadcast=True)

        last_pod_status = ''
        while True:
            pod_list = batch_v1.list_namespaced_pod(namespace=namespace, label_selector=f"job-name={job_name}").items
            if pod_list:
                pod_status = pod_list[0].status.phase
                if last_pod_status != pod_status:
                    emit('status_update', {'message': f'Job status: {pod_status}', 'status': pod_status}, broadcast=True)
                    last_pod_status = pod_status
                with log_stream_lock:
                    if pod_status == "Running" and not log_stream_active:
                        @copy_current_request_context
                        def thread_function():
                            broadcast_logs(app.app_context())

                        log_stream_active = True
                        log_stream_thread = threading.Thread(target=thread_function)
                        log_stream_thread.start()
                        break
            time.sleep(1)

    except client.exceptions.ApiException as e:
        error_body = e.body
        error_reason = e.reason
        emit('status_update', {'message': f"API Error: {error_reason} - {error_body}", 'status': 'Error'}, broadcast=True)

@socketio.on('cancel_job')
def cancel_job():
    global job_name
    try:
        if job_name:
            # Delete the job
            batch_v1.delete_namespaced_job(
                name=job_name,
                namespace=namespace,
                body=client.V1DeleteOptions(propagation_policy='Foreground')
            )
            emit('status_update', {'message': f'Job {job_name} has been canceled.', 'status': 'Canceled'}, broadcast=True)
            job_name = None
        else:
            emit('status_update', {'message': 'No active job found to cancel.', 'status': 'Idle'}, broadcast=True)

    except client.exceptions.ApiException as e:
        emit('status_update', {'message': f"Error: {str(e)}", 'status': 'Error'}, broadcast=True)

def broadcast_logs(context):
    global job_name, log_stream_active
    with context:
        try:
            while True:
                # Check if the job exists
                pod_list = batch_v1.list_namespaced_pod(namespace=namespace, label_selector=f"job-name={job_name}").items
                if not pod_list:
                    log_stream_active = False
                    break

                pod_name = pod_list[0].metadata.name

                try:
                    # Stream logs directly from the Kubernetes API
                    log_stream = v1.read_namespaced_pod_log(
                        name=pod_name,
                        namespace=namespace,
                        follow=True,
                        _preload_content=False
                    ).stream()

                    for line in log_stream:
                        if not log_stream_active:
                            return
                        emit('log_update', {'message': line.decode('utf-8').strip()}, broadcast=True)
                except client.exceptions.ApiException as e:
                    emit('status_update', {'message': f"{str(e)}", 'status': 'Error'}, broadcast=True)

                # Check if the pod is terminated
                pod_status = pod_list[0].status.phase
                if pod_status != "Running":
                    emit('status_update', {'message': f'Pod status: {pod_status}', 'status': pod_status}, broadcast=True)
                    log_stream_active = False
                    break

                time.sleep(1)

        except client.exceptions.ApiException as e:
            emit('log_update', {'message': f"Error: {str(e)}"}, broadcast=True)
            log_stream_active = False

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
