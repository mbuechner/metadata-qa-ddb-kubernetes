import time
import logging
import os
import threading
import json
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

api_client = client.ApiClient()
api_core = client.CoreV1Api()
api_batch = client.BatchV1Api()

# Log stream
log_stream_active = False
log_stream_thread = None
log_stream_lock = threading.Lock()

namespace = "ddbmetadata-qa"
cronjob_name = "ddbmetadata-qa"
job_name = None

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
        cronjob = api_batch.read_namespaced_cron_job(name=cronjob_name, namespace=namespace)

        # Define a unique job name
        job_name = f"{cronjob_name}-{int(time.time())}"

        # Create a new Job spec based on the CronJob
        
        
        job_body = {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {
                "name": job_name,
                "namespace": namespace,
            },
            "spec": api_client.sanitize_for_serialization(cronjob.spec.job_template.spec),
            "restartPolicy": "Never"
        }

        # Create the Job
        api_batch.create_namespaced_job(namespace=namespace, body=job_body)
        emit('status_update', {'message': f'Job {job_name} is starting...', 'status': 'Starting'}, broadcast=True)

        last_pod_status = ''
        while True:
            pod_list = api_core.list_namespaced_pod(namespace=namespace, label_selector=f"job-name={job_name}").items
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
                                broadcast_logs(app.app_context())

                            log_stream_active = True
                            log_stream_thread = threading.Thread(target=thread_function)
                            log_stream_thread.start()
                        break
                    elif pod_status == "Failed":
                        emit('status_update', {'message': f"Starting job failed", 'status': 'Error'}, broadcast=True)  
                        job_name = None
                        break
            time.sleep(1)

    except client.exceptions.ApiException as e:
        error_body = e.body
        error_reason = e.reason
        emit('status_update', {'message': f"API Error: {error_reason} - {error_body}", 'status': 'Error'}, broadcast=True)
        job_name = None

@socketio.on('cancel_job')
def cancel_job():
    global job_name
    try:
        if job_name:
            # Delete the job
            api_batch.delete_namespaced_job(
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
            while log_stream_active:
                # Check if the job exists
                pod_list = api_core.list_namespaced_pod(namespace=namespace, label_selector=f"job-name={job_name}").items
                if not pod_list:
                    log_stream_active = False
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
                            _preload_content=False
                        ).stream()

                        for line in log_stream:
                            if not log_stream_active:
                                return
                            emit('log_update', {'message': line.decode('utf-8').strip()}, broadcast=True)
                    except client.exceptions.ApiException as e:
                        emit('status_update', {'message': f"{str(e)}", 'status': 'Error'}, broadcast=True)

                # Check if the pod is terminated
                if pod_status in ["Failed", "Succeeded"]:
                    emit('status_update', {'message': f'Pod {pod_name} status: {pod_status}', 'status': pod_status}, broadcast=True)
                    log_stream_active = False
                    job_name = None
                    break

                time.sleep(3)

        except client.exceptions.ApiException as e:
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
